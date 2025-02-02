import os
import itertools
import contextlib
from collections import namedtuple
import heapq
import io
import zipfile
import numpy as np
import glob
import posixpath
from scipy import sparse, stats, signal, interpolate, ndimage
import pandas as pd
import powerlaw
from braingeneers.utils import s3wrangler
import braingeneers.utils.smart_open_braingeneers as smart_open
from braingeneers.utils.common_utils import get_basepath
from typing import List, Tuple, Union, Optional, Iterable, Dict, Any
from dataclasses import dataclass
from deprecated import deprecated

__all__ = ['DCCResult', 'read_phy_files', 'SpikeData', 'filter',
           'fano_factors', 'pearson', 'cumulative_moving_average',
           'burst_detection', 'ThresholdedSpikeData', 'NeuronAttributes', 
           'load_spike_data']


DCCResult = namedtuple('DCCResult', 'dcc p_size p_duration')

@dataclass
class NeuronAttributes:
    cluster_id: int
    channel: np.ndarray
    position: Tuple[float, float]
    amplitudes: List[float]
    template: np.ndarray
    templates: np.ndarray
    label: str

    # These lists are the same length and correspond to each other
    neighbor_channels: np.ndarray
    neighbor_positions: List[Tuple[float, float]]
    neighbor_templates: List[np.ndarray]

    def __init__(self, *args, **kwargs):
        self.cluster_id = kwargs.pop("cluster_id")
        self.channel = kwargs.pop("channel")
        self.position = kwargs.pop("position")
        self.amplitudes = kwargs.pop("amplitudes")
        self.template = kwargs.pop("template")
        self.templates = kwargs.pop("templates")
        self.label = kwargs.pop("label")
        self.neighbor_channels = kwargs.pop("neighbor_channels")
        self.neighbor_positions = kwargs.pop("neighbor_positions")
        self.neighbor_templates = kwargs.pop("neighbor_templates")
        for key, value in kwargs.items():
            setattr(self, key, value)

    def add_attribute(self, key, value):
        setattr(self, key, value)

    def list_attributes(self):
        return [attr for attr in dir(self) if not attr.startswith('__') and not callable(getattr(self, attr))]


def list_sorted_files(uuid, basepath=None):
    """
    Lists files in a directory.

    :param path: the path to the directory.
    :param pattern: the pattern to match.
    :return: a list of files.
    """
    if basepath is None:
        basepath = get_basepath()
    if 's3://' in basepath:
        return s3wrangler.list_objects(basepath + 'ephys/' + uuid + '/derived/kilosort2/')
    else:
        # return glob.glob(os.path.join(basepath, f'ephys/{uuid}/derived/kilosort2/*'))
        return glob.glob(basepath + f'ephys/{uuid}/derived/kilosort2/*')


    
    
def load_spike_data(uuid, experiment=None, basepath=None, full_path = None, fs=20000.0,
                    groups_to_load = ["good", "mua", "", "unsorted"], verbose=False):
    """
    Loads spike data from a dataset.

    :param uuid: the UUID for a specific dataset.
    :param experiment: an optional string to specify a particular experiment in the dataset.
    :param basepath: an optional string to specify a basepath for the dataset.
    :return: SpikeData class with a list of spike time lists and a list of NeuronAttributes.
    """

    if basepath is None:
        basepath = get_basepath()

    prefix = f'ephys/{uuid}/derived/kilosort2/{experiment}'
    path = posixpath.join(basepath, prefix)


    if full_path is not None:
        print('Using full path')
        path = full_path
    else:
    
        if path.startswith('s3://'):
            # If path is an s3 path, use wrangler
            file_list = s3wrangler.list_objects(path)

            zip_files = [file for file in file_list if file.endswith('.zip')]

            if not zip_files:
                raise ValueError('No zip files found in specified location.')
            elif len(zip_files) > 1:
                print('Multiple zip files found. Using the first one.')

            path = zip_files[0]

        else:
            # If path is a local path, check locally
            file_list = glob.glob(path + '*.zip')
            

            zip_files = [file for file in file_list if file.endswith('.zip')]

            if not zip_files:
                raise ValueError('No zip files found in specified location.')
            elif len(zip_files) > 1:
                print('Multiple zip files found. Using the first one.')

            path = zip_files[0]

    

    with smart_open.open(path, 'rb') as f0:
        f = io.BytesIO(f0.read())
        if verbose:
            print('Opening zip file...')
        with zipfile.ZipFile(f, 'r') as f_zip:
            assert 'params.py' in f_zip.namelist(), "Wrong spike sorting output."
            if verbose:
                print('Reading params.py...')
            with io.TextIOWrapper(f_zip.open('params.py'), encoding='utf-8') as params:
                for line in params:
                    if "sample_rate" in line:
                        fs = float(line.split()[-1])
            if verbose:
                print('Reading spike data...')
            clusters = np.load(f_zip.open('spike_clusters.npy')).squeeze()
            templates = np.load(f_zip.open('templates.npy'))
            channels = np.load(f_zip.open('channel_map.npy')).squeeze()
            spike_templates = np.load(f_zip.open('spike_templates.npy')).squeeze()
            spike_times = np.load(f_zip.open('spike_times.npy')).squeeze() / fs * 1e3
            positions = np.load(f_zip.open('channel_positions.npy'))
            amplitudes = np.load(f_zip.open("amplitudes.npy")).squeeze()

            if 'cluster_info.tsv' in f_zip.namelist():
                cluster_info = pd.read_csv(f_zip.open('cluster_info.tsv'), sep='\t')
                cluster_id = np.array(cluster_info['cluster_id'])
                labeled_clusters = cluster_id[cluster_info['group'].isin(groups_to_load)]
            else:
                labeled_clusters = np.unique(clusters)
                # Generate blank labels
                cluster_info = pd.DataFrame({"cluster_id": labeled_clusters, "group": [""] * len(labeled_clusters)})

    assert len(labeled_clusters) > 0, "No clusters found."
    if verbose:
        print('Reorganizing data...')
    df = pd.DataFrame({"clusters": clusters, "spikeTimes": spike_times, "amplitudes": amplitudes})
    cluster_agg = df.groupby("clusters").agg({"spikeTimes": lambda x: list(x),
                                              "amplitudes": lambda x: list(x)})
    cluster_agg = cluster_agg[cluster_agg.index.isin(labeled_clusters)]
    cls_temp = dict(zip(clusters, spike_templates))

    if verbose:
        print('Creating neuron attributes...')
    neuron_attributes = []
    
    for i in range(len(labeled_clusters)):
        c = labeled_clusters[i]
        nbgh_chan_idx = np.nonzero(templates[cls_temp[c]].any(0))[0]
        nbgh_temps = np.transpose(templates[cls_temp[c]][:, templates[cls_temp[c]].any(0)])
        best_chan_idx = np.argmax(np.ptp(nbgh_temps, axis=1))
        best_chan_temp = nbgh_temps[best_chan_idx]
        best_channel = channels[nbgh_chan_idx[best_chan_idx]]
        best_position = tuple(positions[nbgh_chan_idx[best_chan_idx]])
        neuron_attributes.append(
            NeuronAttributes(
                cluster_id=c,
                channel=best_channel,
                position=best_position,
                amplitudes=cluster_agg["amplitudes"][c],
                template=best_chan_temp,
                templates=templates[cls_temp[c]].T,
                label = cluster_info['group'][cluster_info['cluster_id'] == c].values[0],
                neighbor_channels=channels[nbgh_chan_idx],
                neighbor_positions=[tuple(positions[idx]) for idx in nbgh_chan_idx],
                neighbor_templates=[templates[cls_temp[c]].T[n] for n in nbgh_chan_idx]
            )
        )

    if verbose:
        print('Creating spike data...')
    spike_data = SpikeData(cluster_agg["spikeTimes"].to_list(), neuron_attributes = neuron_attributes)

    if verbose:
        print('Done.')
    return spike_data



def read_phy_files(path: str, fs=20000.0):
    """
    :param path: a s3 or local path to a zip of phy files.
    :return: SpikeData class with a list of spike time lists and neuron_data.
            neuron_data = {0: neuron_dict, 1: config_dict}
            neuron_dict = {"new_cluster_id": {"channel": c, "position": (x, y),
                            "amplitudes": [a0, a1, an], "template": [t0, t1, tn],
                            "neighbor_channels": [c0, c1, cn],
                            "neighbor_positions": [(x0, y0), (x1, y1), (xn,yn)],
                            "neighbor_templates": [[t00, t01, t0n], [tn0, tn1, tnn]}}
            config_dict = {chn: pos}
    """
    assert path[-3:] == 'zip', 'Only zip files supported!'
    import braingeneers.utils.smart_open_braingeneers as smart_open
    with smart_open.open(path, 'rb') as f0:
        f = io.BytesIO(f0.read())
        
        with zipfile.ZipFile(f, 'r') as f_zip:
            assert 'params.py' in f_zip.namelist(), "Wrong spike sorting output."
            with io.TextIOWrapper(f_zip.open('params.py'), encoding='utf-8') as params:
                for line in params:
                    if "sample_rate" in line:
                        fs = float(line.split()[-1])
            clusters = np.load(f_zip.open('spike_clusters.npy')).squeeze()
            templates = np.load(f_zip.open('templates.npy'))  # (cluster_id, samples, channel_id)
            channels = np.load(f_zip.open('channel_map.npy')).squeeze()
            spike_templates = np.load(f_zip.open('spike_templates.npy')).squeeze()
            spike_times = np.load(f_zip.open('spike_times.npy')).squeeze() / fs * 1e3  # in ms
            positions = np.load(f_zip.open('channel_positions.npy'))
            amplitudes = np.load(f_zip.open("amplitudes.npy")).squeeze()
            if 'cluster_info.tsv' in f_zip.namelist():
                cluster_info = pd.read_csv(f_zip.open('cluster_info.tsv'), sep='\t')
                cluster_id = np.array(cluster_info['cluster_id'])
                # select clusters using curation label, remove units labeled as "noise"
                # find the best channel by amplitude
                labeled_clusters = cluster_id[cluster_info['group'] != "noise"]
            else:
                labeled_clusters = np.unique(clusters)

    df = pd.DataFrame({"clusters": clusters, "spikeTimes": spike_times, "amplitudes": amplitudes})
    cluster_agg = df.groupby("clusters").agg({"spikeTimes": lambda x: list(x),
                                              "amplitudes": lambda x: list(x)})
    cluster_agg = cluster_agg[cluster_agg.index.isin(labeled_clusters)]

    cls_temp = dict(zip(clusters, spike_templates))
    neuron_dict = dict.fromkeys(np.arange(len(labeled_clusters)), None)

    neuron_attributes = []
    for i in range(len(labeled_clusters)):
        c = labeled_clusters[i]
        nbgh_chan_idx = np.nonzero(templates[cls_temp[c]].any(0))[0]
        nbgh_temps = np.transpose(templates[cls_temp[c]][:, templates[cls_temp[c]].any(0)])
        best_chan_idx = np.argmax(np.ptp(nbgh_temps, axis=1))
        best_chan_temp = nbgh_temps[best_chan_idx]
        nbgh_channels = channels[nbgh_chan_idx]
        nbgh_postions = [tuple(positions[idx]) for idx in nbgh_chan_idx]
        best_channel = channels[nbgh_chan_idx[best_chan_idx]]
        best_position = tuple(positions[nbgh_chan_idx[best_chan_idx]])
        cls_amp = cluster_agg["amplitudes"][c]
        neuron_dict[i] = {"channel": best_channel, "position": best_position,
                          "amplitudes": cls_amp, "template": best_chan_temp,
                          "neighbor_channels": nbgh_channels, "neighbor_positions": nbgh_postions,
                          "neighbor_templates": nbgh_temps}
        neuron_attributes.append(
            NeuronAttributes(
                cluster_id=c,
                channel=best_channel,
                position=best_position,
                amplitudes=cluster_agg["amplitudes"][c],
                template=best_chan_temp,
                templates=templates[cls_temp[c]].T,
                label = cluster_info['group'][cluster_info['cluster_id'] == c].values[0],
                neighbor_channels=channels[nbgh_chan_idx],
                neighbor_positions=[tuple(positions[idx]) for idx in nbgh_chan_idx],
                neighbor_templates=[templates[cls_temp[c]].T[n] for n in nbgh_chan_idx]
            )
        )

    config_dict = dict(zip(channels, positions))
    neuron_data = {0: neuron_dict}
    metadata = {0: config_dict}
    spikedata = SpikeData(list(cluster_agg["spikeTimes"]), neuron_data=neuron_data, metadata=metadata, neuron_attributes=neuron_attributes)
    return spikedata



class SpikeData:
    """
    Class for handling and manipulating neuronal spike data.

    This class provides a way to load, process, and analyze spike
    data from different input types, including NEST spike recorder,
    lists of indices and times, lists of channel-time pairs, lists of
    Neuron objects, or even prebuilt spike trains. 

    Each instance of SpikeData has the following attributes:

    - train: The main data attribute. This is a list of numpy arrays, 
      where each array contains the spike times for a particular neuron.

    - N: The number of neurons in the dataset.

    - length: The length of the spike train, defaults to the time of 
      the last spike.

    - neuron_attributes: A list of neuronAttributes for each neuron.
      spikeData.neuron_attributes[i].template is the neuronAttributes object
      for neuron i, specifically for the template feature.

    - neuron_data: A dictionary where each key-value pair represents 
      an additional attribute of neurons.

    - metadata: A dictionary containing any additional information or 
      metadata about the spike data.

    - raw_data: If provided, this numpy array contains the raw time 
      series data.

    - raw_time: This is either a numpy array of sample times, or a 
      single float representing a sample rate in kHz.

    In addition to these data attributes, the SpikeData class also 
    provides some useful methods for working with spike data, such as 
    iterating through spike times or (index, time) pairs for all units 
    in time order.

    Note that SpikeData expects spike times to be in units of 
    milliseconds, unless a list of Neuron objects is given; these have 
    spike times in units of samples, which are converted to 
    milliseconds using the sample rate saved in the Neuron object.
    """

    def __init__(self, arg1, arg2=None, *, N=None, length=None,
                 neuron_attributes = [], neuron_data={}, metadata={},
                 raw_data=None, raw_time=None):
        '''
        Parses different argument list possibilities into the desired
        format: a list indexed by unit ID, where each element is a list of
        spike times. The five possibilities accepted are: (1) a pair of
        lists corresponding to unit indices and times, (2) a NEST spike
        recorder plus the collection of nodes to record from, (3) a list of
        lists of spike times, (4) a list of channel-time pairs, (5) a list
        of Neuron objects whose parameter spike_time is a list of spike
        times. Metadata can also be passed in to the constructor, on
        a global basis in a dict called `metadata` or on a per-neuron basis
        in a dict of lists `neuron_data`.

        Arbitrary raw timeseries data, not associated with particular units,
        can be passed in as `raw_data`, an array whose last dimension
        corresponds to the times given in `raw_time`. The `raw_time` argument
        can also be a sample rate in kHz, in which case it is generated
        assuming that the start of the raw data corresponds with t=0.

        Spike times should be in units of milliseconds, unless a list of
        Neurons is given; these have spike times in units of samples, which
        are converted to milliseconds using the sample rate saved in the
        Neuron object.
        '''
        # Install the metadata and neuron_data.
        self.metadata = metadata.copy()
        self.neuron_attributes = neuron_attributes.copy()
        self._neuron_data = neuron_data.copy()

        # If two arguments are provided, they're either a NEST spike
        # detector plus NodeCollection, or just a list of indices and
        # times.
        if arg2 is not None:

            # First, try parsing spikes from a NEST spike detector. Accept
            # either a number of cells or a NodeCollection as arg2.
            try:
                times = arg1.events['times']
                idces = arg1.events['senders']
                try:
                    maxcell = arg2
                    cells = np.arange(maxcell) + 1
                except (TypeError, ValueError):
                    cells = np.array(arg2)
                    maxcell = cells.max()
                cellrev = np.zeros(maxcell + 1, int)
                cellrev[cells] = np.arange(len(cells))

                # Store the underlying NEST cell IDs in the neuron_data.
                self._neuron_data['nest_id'] = cells

                cellset = set(cells)
                self.train = [[] for _ in cells]
                for i, t in zip(idces, times):
                    if i in cellset:
                        self.train[cellrev[i]].append(t)

            # If that fails, we must have lists of indices and times.
            except AttributeError:
                self.train = _train_from_i_t_list(arg1, arg2, N)

        else:
            # The input could be a list [musclebeachtools.Neuron]
            try:
                self.train = [np.asarray(n.spike_time) / n.fs * 1e3
                              for n in arg1]

            # Now it could be either (channel, time) pairs or
            # a complete prebuilt spike train.
            except AttributeError:

                # If all the elements are length 2, it must be pairs.
                if all([len(arg) == 2 for arg in arg1]):
                    idces = [i for i, _ in arg1]
                    times = [t for i, t in arg1]
                    self.train = _train_from_i_t_list(idces, times, N)
                # Otherwise, it's just a plain spike train.
                else:
                    self.train = arg1

        # Make sure each individual spike train is sorted, because
        # none of the formats guarantee this but all the algorithms
        # expect it. This also copies each array to avoid aliasing.
        self.train = [np.sort(times) for times in self.train]

        # The length of the spike train defaults to the last spike
        # time it contains.
        if length is None:
            length = max((t[-1] for t in self.train if len(t) > 0))
        self.length = length

        # If a number of units was provided, make the list of spike
        # trains consistent with that number.
        if N is not None and len(self.train) < N:
            self.train += [np.array([], float) for _ in
                           range(N - len(self.train))]
        self.N = len(self.train)

        # Add the raw data if present, including generating raw time.
        if (raw_data is None) != (raw_time is None):
            raise ValueError('Must provide both or neither of '
                             '`raw_data` and `raw_time`.')
        if raw_data is not None:
            self.raw_data = np.asarray(raw_data)
            self.raw_time = np.asarray(raw_time)
            if self.raw_time.shape == ():
                self.raw_time = np.arange(self.raw_data.shape[-1]) / raw_time
            elif self.raw_data.shape[-1:] != self.raw_time.shape:
                raise ValueError('Length of `raw_data` and '
                                 '`raw_time` must match.')
        else:
            self.raw_data = np.zeros((0, 0))
            self.raw_time = np.zeros((0,))

        # Double-check that the neuron_data has the right number of values.
        for k, values in self._neuron_data.items():
            if len(values) != self.N:
                raise ValueError(f'Malformed metadata: neuron_data[{k}]'
                                 f'should have {self.N} items.')

    @property
    def times(self):
        'Iterate spike times for all units in time order.'
        return heapq.merge(*self.train)

    @property
    def events(self):
        'Iterate (index,time) pairs for all units in time order.'
        return heapq.merge(*[zip(itertools.repeat(i), t)
                             for (i, t) in enumerate(self.train)],
                           key=lambda x: x[1])
        
    @property
    @deprecated('Use NeuronAttributes instead of neuron_data, with the function load_spike_data()')
    def neuron_data(self):
        return self._neuron_data

    def idces_times(self):
        '''
        Return separate lists of times and indices, e.g. for raster
        plots. This is not a property unlike `times` and `events`
        because the lists must actually be constructed in memory.
        '''
        idces, times = [], []
        for i, t in self.events:
            idces.append(i)
            times.append(t)
        return np.array(idces), np.array(times)

    def frames(self, length, overlap=0):
        '''
        Iterate new SpikeData objects corresponding to subwindows of
        a given `length` with a fixed `overlap`.
        '''
        for start in np.arange(0, self.length, length - overlap):
            yield self.subtime(start, start + length)

    def binned(self, bin_size=40):
        '''
        Quantizes time into intervals of bin_size and counts the
        number of events in each bin, considered as a lower half-open
        interval of times, with the exception that events at time
        precisely zero will be included in the first bin.
        '''
        return self.sparse_raster(bin_size).sum(0)

    def rates(self, unit='kHz'):
        '''
        Calculate the firing rate of each neuron as an average number
        of events per time over the duration of the data. The unit may
        be either `Hz` or `kHz` (default).
        '''
        rates = np.array([len(t) for t in self.train]) / self.length

        if unit == 'Hz':
            return 1e3 * rates
        elif unit == 'kHz':
            return rates
        else:
            raise ValueError(f'Unknown unit {unit} (try Hz or kHz)')

    def resampled_isi(self, times, sigma_ms=10.0):
        '''
        Calculate firing rate at the given times by interpolating the
        inverse ISI, considered valid in between any two spikes. If any
        neuron has only one spike, the rate is assumed to be zero.
        '''
        return np.array([_resampled_isi(t, times, sigma_ms)
                         for t in self.train])

    def subset(self, units, by=None):
        '''
        Return a new SpikeData with spike times for only some units,
        selected either by their indices or by an ID stored under a given
        key in the neuron_data. If IDs are not unique, every neuron which
        matches is included in the output. Metadata and raw data are
        propagated exactly, while neuron data is subsetted in the same way
        as the spike trains.
        '''
        # The inclusion condition depends on whether we're selecting by ID
        # or by index.
        if by is None:
            def cond(i):
                return i in units
        else:
            def cond(i):
                return self.neuron_data[by][i] in units

        train = [ts for i, ts in enumerate(self.train) if cond(i)]
        neuron_data = {k: [v for i, v in enumerate(vs) if cond(i)]
                       for k, vs in self.neuron_data.items()}
                       
        neuron_attributes = []
        if len(self.neuron_attributes) >= len(units):
            neuron_attributes = [self.neuron_attributes[i] for i in units] # TODO work with by

        
        return SpikeData(train, length=self.length, N=len(train),
                         neuron_attributes=neuron_attributes,
                         neuron_data=neuron_data,
                         metadata=self.metadata,
                         raw_time=self.raw_time,
                         raw_data=self.raw_data)

    def subtime(self, start, end):
        '''
        Return a new SpikeData with only spikes in a time range,
        closed on top but open on the bottom unless the lower bound is
        zero, consistent with the binning methods. This is to ensure
        no overlap between adjacent slices.
        Start and end can be negative, in which case they are counted
        backwards from the end. They can also be None or Ellipsis,
        which results in only paying attention to the other bound.
        All metadata and neuron data are propagated, while raw data is
        sliced to the same range of times, but overlap is okay, so we
        include all samples within the closed interval.
        '''
        if start is None or start is Ellipsis:
            start = 0
        elif start < 0:
            start += self.length

        if end is None or end is Ellipsis:
            end = self.length
        elif end < 0:
            end += self.length
        elif end > self.length:
            end = self.length

        # Special case out the start=0 case by nopping the comparison.
        lower = start if start > 0 else -np.inf

        # Subset the spike train by time.
        train = [t[(t > lower) & (t <= end)] - start
                 for t in self.train]

        # Subset and propagate the raw data.
        rawmask = (self.raw_time >= lower) & (self.raw_time <= end)
        return SpikeData(train, length=end - start, N=self.N,
                         neuron_attributes=self.neuron_attributes,
                         neuron_data=self.neuron_data,
                         metadata=self.metadata,
                         raw_time=self.raw_time[rawmask] - start,
                         raw_data=self.raw_data[..., rawmask])

    def __getitem__(self, key):
        '''
        If a slice is provided, it is taken in time as with self.subtime(),
        but if an iterable is provided, it is taken as a list of neuron
        indices to select as with self.subset().
        '''
        if isinstance(key, slice):
            return self.subtime(key.start, key.stop)
        else:
            return self.subset(key)


    def append(self, spikeData, offset=0):
        '''Appends a spikeData object to the current object. These must have
        the same number of neurons.

        :param: spikeData: spikeData object to append to the current object
        '''
        assert self.N == spikeData.N, 'Number of neurons must be the same'
        train = ([np.hstack([tr1, tr2 + self.length + offset]) for tr1, tr2 in zip(self.train,spikeData.train)])
        raw_data = np.concatenate((self.raw_data, spikeData.raw_data), axis=1)
        raw_time = np.concatenate((self.raw_time, spikeData.raw_time))
        length = self.length + spikeData.length + offset
        # TODO: Concatenate meta data, neuron data, and neuron attributes
        #metadata = self.metadata + spikeData.metadata
        #neuron_data = self.neuron_data + spikeData.neuron_data
        return SpikeData(train, length=length, N=self.N,
            neuron_attributes=self.neuron_attributes,
            neuron_data=self.neuron_data,
            raw_time=raw_time, raw_data=raw_data)



    def sparse_raster(self, bin_size=20):
        '''
        Bin all spike times and create a sparse array where entry
        (i,j) is the number of times cell i fired in bin j. Bins are
        left-open and right-closed intervals except the first, which
        will capture any spikes occurring exactly at t=0.
        '''
        indices = np.hstack([np.ceil(ts / bin_size) - 1
                             for ts in self.train]).astype(int)
        units = np.hstack([0] + [len(ts) for ts in self.train])
        indptr = np.cumsum(units)
        values = np.ones_like(indices)
        length = int(np.ceil(self.length / bin_size))
        np.clip(indices, 0, length - 1, out=indices)
        ret = sparse.csr_array((values, indices, indptr),
                               shape=(self.N, length))
        return ret

    def raster(self, bin_size=20):
        '''
        Bin all spike times and create a dense array where entry
        (i,j) is the number of times cell i fired in bin j.
        '''
        return self.sparse_raster(bin_size).toarray()

    def interspike_intervals(self):
        'Produce a list of arrays of interspike intervals per unit.'
        return [np.diff(ts) for ts in self.train]

    def isi_skewness(self):
        'Skewness of interspike interval distribution.'
        intervals = self.interspike_intervals()
        return [stats.skew(intl) for intl in intervals]

    def isi_log_histogram(self, bin_num=300):
        '''
        Logarithmic (log base 10) interspike interval histogram.
        Return histogram and bins in log10 scale.
        '''
        intervals = self.interspike_intervals()
        ret = []
        ret_logbins = []
        for ts in intervals:
            log_bins = np.geomspace(min(ts), max(ts), bin_num + 1)
            hist, _ = np.histogram(ts, log_bins)
            ret.append(hist)
            ret_logbins.append(log_bins)
        return ret, ret_logbins

    def isi_threshold_cma(self, hist, bins, coef=1):
        '''
        Calculate interspike interval threshold from cumulative moving
        average [1]. The threshold is the bin that has the max CMA on
        the interspike interval histogram. Histogram and bins are
        logarithmic by default. `coef` is an input variable for
        threshold.
        [1] Kapucu, et al. Frontiers in computational neuroscience 6 (2012): 38
        '''
        isi_thr = []
        for n in range(len(hist)):
            h = hist[n]
            max_idx = 0
            cma = 0
            cma_list = []
            for i in range(len(h)):
                cma = (cma * i + h[i]) / (i + 1)
                cma_list.append(cma)
            max_idx = np.argmax(cma_list)
            thr = (bins[n][max_idx + 1]) * coef
            isi_thr.append(thr)
        return isi_thr

    def burstiness_index(self, bin_size=40):
        '''
        Compute the burstiness index [1], a number from 0 to 1 which
        quantifies synchronization of activity in neural cultures.
        Spikes are binned, and the fraction of spikes accounted for by
        the top 15% will be 0.15 if activity is fully asynchronous, and
        1.0 if activity is fully synchronized into just a few bins. This
        is linearly rescaled to the range 0--1 for clearer interpretation.
        [1] Wagenaar, Madhavan, Pine & Potter. Controlling bursting
            in cortical cultures with closed-loop multi-electrode
            stimulation. J Neurosci 25:3, 680–688 (2005).
        '''
        binned = self.binned(bin_size)
        binned.sort()
        N85 = int(np.round(len(binned) * 0.85))

        if N85 == len(binned):
            return 1.0
        else:
            f15 = binned[N85:].sum() / binned.sum()
            return (f15 - 0.15) / 0.85

    def concatenate_spike_data(self, sd):
        '''
        Adds neurons from sd to this spike data object.
        '''
        
        if sd.length == self.length:
            self.train += sd.train
            self.N += sd.N
            self.raw_data += sd.raw_data
            self.raw_time += sd.raw_time
            # TODO: Consider the case where two separate neurons have the same index!
            self.neuron_data.update(sd.neuron_data)
            self.metadata.update(sd.metadata)
            self.neuron_attributes += sd.neuron_attributes
        else:
            sd = sd.subtime(0, self.length)
            self.train += sd.train
            self.N += sd.N
            self.raw_data += sd.raw_data
            self.raw_time += sd.raw_time
            # TODO: Consider the case where two separate neurons have the same index!
            self.neuron_data.update(sd.neuron_data)
            self.metadata.update(sd.metadata)
            self.neuron_attributes += sd.neuron_attributes


    def spike_time_tilings(self, delt=20):
        '''
        Compute the full spike time tiling coefficient matrix.
        '''
        ret = np.diag(np.ones(self.N))
        for i in range(self.N):
            for j in range(i + 1, self.N):
                ret[i, j] = ret[j, i] = self.spike_time_tiling(i, j, delt)
        return ret

    def spike_time_tiling(self, i, j, delt=20):
        '''
        Given the indices of two units of interest, compute the spike
        time tiling coefficient [1], a metric for causal relationships
        between spike trains with some improved intuitive properties
        compared to the Pearson correlation coefficient.
        [1] Cutts & Eglen. Detecting pairwise correlations in spike
            trains: An objective comparison of methods and application
            to the study of retinal waves. J Neurosci 34:43,
            14288–14303 (2014).
        '''
        tA, tB = self.train[i], self.train[j]

        if len(tA) == 0 or len(tB) == 0:
            return 0.0

        TA = _sttc_ta(tA, delt, self.length) / self.length
        TB = _sttc_ta(tB, delt, self.length) / self.length

        PA = _sttc_na(tA, tB, delt) / len(tA)
        PB = _sttc_na(tB, tA, delt) / len(tB)

        aa = (PA - TB) / (1 - PA * TB) if PA * TB != 1 else 0
        bb = (PB - TA) / (1 - PB * TA) if PB * TA != 1 else 0
        return (aa + bb) / 2

    def avalanches(self, thresh, bin_size=40):
        '''
        Bin the spikes in this data, and group the result into lists
        corresponding to avalanches, defined as deviations above
        a given threshold spike count.
        '''
        counts = self.binned(bin_size)
        active = counts > thresh
        toggles = np.where(np.diff(active))[0]

        # If we start inactive, the first toggle begins the first
        # avalanche. Otherwise, we have to ignore it because we don't
        # know how long the system was active before.
        if active[0]:
            ups = toggles[1::2]
            downs = toggles[2::2]
        else:
            ups = toggles[::2]
            downs = toggles[1::2]

        # Now batch up the transitions and create a list of spike
        # counts in between them.
        return [counts[up + 1:down + 1] for up, down in zip(ups, downs)]

    def avalanche_duration_size(self, thresh, bin_size=40):
        '''
        Collect the avalanches in this data and regroup them into
        a pair of lists: durations and sizes.
        '''
        durations, sizes = [], []
        for avalanche in self.avalanches(thresh, bin_size):
            durations.append(len(avalanche))
            sizes.append(sum(avalanche))
        return np.array(durations), np.array(sizes)

    def deviation_from_criticality(self, quantile=0.35, bin_size=40,
                                   N=1000, pval_truncated=0.05):
        '''
        Calculates the deviation from criticality according to the
        method of Ma et al. (2019), who used the relationship of the
        dynamical critical exponent to the exponents of the separate
        power laws corresponding to the avalanche size and duration
        distributions as a metric for suboptimal cortical function
        following monocular deprivation.
        The returned DCCResult struct contains not only the DCC metric
        itself but also the significance of the hypothesis that the
        size and duration distributions of the extracted avalanches
        are poorly fit by power laws.
        [1] Ma, Z., Turrigiano, G. G., Wessel, R. & Hengen, K. B.
            Cortical circuit dynamics are homeostatically tuned to
            criticality in vivo. Neuron 104, 655-664.e4 (2019).
        '''
        # Calculate the spike count threshold corresponding to
        # the given quantile.
        thresh = np.quantile(self.binned(bin_size), quantile)

        # Gather durations and sizes. If there are no avalanches, we
        # very much can't say the system is critical.
        durations, sizes = self.avalanche_duration_size(thresh, bin_size)
        if len(durations) == 0:
            return DCCResult(dcc=np.inf, p_size=1.0, p_duration=1.0)

        # Call out to all the actual statistics.
        p_size, alpha_size = _p_and_alpha(sizes, N, pval_truncated)
        p_dur, alpha_dur = _p_and_alpha(durations, N, pval_truncated)

        # Fit and predict the dynamical critical exponent.
        τ_fit = np.polyfit(np.log(durations), np.log(sizes), 1)[0]
        τ_pred = (alpha_dur - 1) / (alpha_size - 1)
        dcc = abs(τ_pred - τ_fit)

        # Return the DCC value and significance.
        return DCCResult(dcc=dcc, p_size=p_size, p_duration=p_dur)


    def latencies(self, times, window_ms=100):
        '''
        Given a sorted list of times, compute the latencies from that time to
        each spike in the train within a window
        :param times: list of times
        :param window_ms: window in ms
        :return: 2d list, each row is a list of latencies
                        from a time to each spike in the train
        '''
        latencies = []
        if len(times) == 0:
            return latencies
        
        for train in self.train:
            cur_latencies = []
            if len(train) == 0:
                latencies.append(cur_latencies)
                continue
            for time in times:
                # Subtract time from all spikes in the train
                # and take the absolute value
                abs_diff_ind = np.argmin(np.abs(train - time))
                
                # Calculate the actual latency
                latency = np.array(train)-time
                latency = latency[abs_diff_ind]

                abs_diff = np.abs(latency)
                if abs_diff <= window_ms:
                    cur_latencies.append(latency)
            latencies.append(cur_latencies)
        return latencies

    def latencies_to_index(self, i, window_ms=100):
        '''
        Given an index, compute latencies using self.latencies()
        :param i: index of the unit
        :param window_ms: window in ms
        :return: 2d list, each row is a list of latencies per neuron
        '''

        return self.latencies(self.train[i], window_ms)

    

    
            


def filter(raw_data, fs_Hz=20000, filter_order=3, filter_lo_Hz=300,
           filter_hi_Hz=6000, time_step_size_s=10, channel_step_size=100,
           verbose=0, zi=None, return_zi=False):
    '''
    Filter the raw data using a bandpass filter.

    :param raw_data: [channels, time] array of raw ephys data
    :param fs_Hz: sampling frequency of raw data in Hz
    :param filter_order: order of the filter
    :param filter_lo_Hz: low frequency cutoff in Hz
    :param filter_hi_Hz: high frequency cutoff in Hz
    :param filter_step_size_s: size of chunks to filter in seconds
    :param channel_step_size: number of channels to filter at once
    :param verbose: verbosity level
    :param zi: initial conditions for the filter
    :param return_zi: whether to return the final filter conditions

    :return: filtered data
    '''


    time_step_size = int(time_step_size_s * fs_Hz)
    data = np.zeros_like(raw_data)


    # Get filter params
    b, a = signal.butter(fs=fs_Hz, btype='bandpass', #output='sos',
                        N=filter_order, Wn=[filter_lo_Hz, filter_hi_Hz])

    if zi is None:
        # Filter initial state
        zi = signal.lfilter_zi(b, a)
        zi = np.vstack([zi*np.mean(raw_data[ch,:5])
                        for ch in range(raw_data.shape[0])])

    # Step through the data in chunks and filter it
    for ch_start in range(0, raw_data.shape[0], channel_step_size):
        ch_end = min(ch_start + channel_step_size, raw_data.shape[0])

        if verbose:
            print(f'Filtering channels {ch_start} to {ch_end}')

        for t_start in range(0, raw_data.shape[1], time_step_size):
            t_end = min(t_start + time_step_size, raw_data.shape[1])

            data[ch_start:ch_end, t_start:t_end], zi[ch_start:ch_end,:] = signal.lfilter(
                    b, a, raw_data[ch_start:ch_end, t_start:t_end],
                    axis=1, zi=zi[ch_start:ch_end,:])

    return data if not return_zi else (data, zi)


def _resampled_isi(spikes, times, sigma_ms):
    '''
    Calculate the firing rate of a spike train at specific times, based on
    the reciprocal inter-spike interval. It is assumed to have been sampled
    halfway between any two given spikes, interpolated, and then smoothed by
    a Gaussian kernel with the given width.
    '''
    if len(spikes) == 0:
        return np.zeros_like(times)
    elif len(spikes) == 1:
        return np.ones_like(times) / spikes[0]
    else:
        x = 0.5*(spikes[:-1] + spikes[1:])
        y = 1/np.diff(spikes)
        fr = np.interp(times, x, y)
        if len(np.atleast_1d(fr)) < 2:
            return fr

        dt_ms = times[1] - times[0]
        sigma = sigma_ms / dt_ms
        if sigma > 0:
            return ndimage.gaussian_filter1d(fr, sigma)
        else:
            return fr


def _p_and_alpha(data, N_surrogate=1000, pval_truncated=0.0):
    '''
    Perform a power-law fit to some data, and return a p-value for the
    hypothesis that this fit is poor, together with just the exponent
    of the fit.

    A positive value of `pval_truncated` means to allow the hypothesis
    of a truncated power law, which must be better than the plain
    power law with the given significance under powerlaw's default
    nested hypothesis comparison test.

    The returned significance value is computed by sampling N surrogate
    datasets and counting what fraction are further from the fitted
    distribution according to the one-sample Kolmogorov-Smirnoff test.
    '''
    # Perform the fits and compare the distributions with IO
    # silenced because there's no option to disable printing
    # in this library...
    with open(os.devnull, 'w') as f, \
            contextlib.redirect_stdout(f), \
            contextlib.redirect_stderr(f):
        fit = powerlaw.Fit(data)
        stat, p = fit.distribution_compare('power_law',
                                           'truncated_power_law',
                                           nested=True)

    # If the truncated power law is a significantly better
    # explanation of the data, use it.
    if stat < 0 and p < pval_truncated:
        dist = fit.truncated_power_law
    else:
        dist = fit.power_law

    # The p-value of the fit is the fraction of surrogate
    # datasets which it fits worse than the input dataset.
    ks = stats.ks_1samp(data, dist.cdf)
    p = np.mean([stats.ks_1samp(dist.generate_random(len(data)),
                                dist.cdf) > ks
                 for _ in range(N_surrogate)])
    return p, dist.alpha


def _train_from_i_t_list(idces, times, N):
    '''
    Given lists of spike times and indices, produce a list whose
    ith entry is a list of the spike times of the ith unit.
    '''
    idces, times = np.asarray(idces), np.asarray(times)
    if N is None:
        N = idces.max() + 1

    ret = []
    for i in range(N):
        ret.append(times[idces == i])
    return ret


def fano_factors(raster):
    '''
    Given arrays of spike times and the corresponding units which
    produced them, computes the Fano factor of the corresponding spike
    raster.

    If a unit doesn't fire, a Fano factor of 1 is returned because in
    the limit of events happening at a rate ε->0, either as
    a Bernoulli process or in the many-bins limit of a single event,
    the Fano factor converges to 1.
    '''
    if sparse.issparse(raster):
        mean = np.array(raster.mean(1)).ravel()
        moment = np.array(raster.multiply(raster).mean(1)).ravel()

        # Silly numbers to make the next line return f=1 for a unit
        # that never spikes.
        moment[mean == 0] = 2
        mean[mean == 0] = 1

        # This is the variance/mean ratio computed in a sparse-friendly
        # way. This algorithm is numerically unstable in general, but
        # should only be a problem if your bin size is way too big.
        return moment/mean - mean

    else:
        mean = np.asarray(raster).mean(1)
        var = np.asarray(raster).var(1)
        mean[mean == 0] = var[mean == 0] = 1.0
        return var / mean


def _sttc_ta(tA, delt, tmax):
    '''
    Helper function for spike time tiling coefficients: calculate the
    total amount of time within a range delt of spikes within the
    given sorted list of spike times tA.
    '''
    if len(tA) == 0:
        return 0

    base = min(delt, tA[0]) + min(delt, tmax - tA[-1])
    return base + np.minimum(np.diff(tA), 2*delt).sum()


def _sttc_na(tA, tB, delt):
    '''
    Helper function for spike time tiling coefficients: given two
    sorted lists of spike times, calculate the number of spikes in
    spike train A within delt of any spike in spike train B.
    '''
    if len(tB) == 0:
        return 0
    tA, tB = np.asarray(tA), np.asarray(tB)

    # Find the closest spike in B after spikes in A.
    iB = np.searchsorted(tB, tA)

    # Clip to ensure legal indexing, then check the spike at that
    # index and its predecessor to see which is closer.
    np.clip(iB, 1, len(tB)-1, out=iB)
    dt_left = np.abs(tB[iB] - tA)
    dt_right = np.abs(tB[iB-1] - tA)

    # Return how many of those spikes are actually within delt.
    return (np.minimum(dt_left, dt_right) <= delt).sum()


def pearson(spikes):
    '''
    Compute a Pearson correlation coefficient matrix for a spike
    raster. Includes a sparse-friendly method for very large spike
    rasters, but falls back on np.corrcoef otherwise because this
    method can be numerically unstable.
    '''
    if not sparse.issparse(spikes):
        return np.corrcoef(spikes)

    Exy = (spikes @ spikes.T) / spikes.shape[1]
    Ex = spikes.mean(axis=1)
    Ex2 = (spikes**2).mean(axis=1)
    σx = np.sqrt(Ex2 - Ex**2)

    # Some cells won't fire in the whole observation window. To get their
    # correlation coefficients to zero, give them infinite σ.
    σx[σx == 0] = np.inf

    # This is by the formula, but there's also a hack to deal with the
    # numerical issues that break the invariant that every variable
    # should have a Pearson autocorrelation of 1.
    Exx = np.multiply.outer(Ex, Ex)
    σxx = np.multiply.outer(σx, σx)
    corr = np.array(Exy - Exx) / σxx
    np.fill_diagonal(corr, 1)
    return corr


def cumulative_moving_average(hist):
    'The culmulative moving average for a histogram. Return a list of CMA.'
    ret = []
    for h in hist:
        cma = 0
        cma_list = []
        for i in range(len(h)):
            cma = (cma * i + h[i]) / (i+1)
            cma_list.append(cma)
        ret.append(cma_list)
    return ret


def burst_detection(spike_times, burst_threshold, spike_num_thr=3):
    '''
    Detect burst from spike times with a interspike interval
    threshold (burst_threshold) and a spike number threshold (spike_num_thr).
    Returns:
        spike_num_list -- a list of burst features
          [index of burst start point, number of spikes in this burst]
        burst_set -- a list of spike times of all the bursts.
    '''
    spike_num_burst = 1
    spike_num_list = []
    for i in range(len(spike_times)-1):
        if spike_times[i+1] - spike_times[i] <= burst_threshold:
            spike_num_burst += 1
        else:
            if spike_num_burst >= spike_num_thr:
                spike_num_list.append([i-spike_num_burst+1, spike_num_burst])
                spike_num_burst = 1
            else:
                spike_num_burst = 1
    burst_set = []
    for loc in spike_num_list:
        for i in range(loc[1]):
            burst_set.append(spike_times[loc[0]+i])
    return spike_num_list, burst_set




class ThresholdedSpikeData(SpikeData):
    '''
    SpikeData generated by applying filtering and thresholding to raw ephys
    data in [channels, time] format.
    '''

    def __init__(self, raw_data, fs_Hz=20000, threshold_sigma=5,
                 filter_order=3, filter_lo_Hz=300, filter_hi_Hz=6000,
                 time_step_size_s=10, do_filter=True, hysteresis=True,
                 direction='both'):
        '''
        :param raw_data: [channels, time] array of raw ephys data
        :param fs_Hz: sampling frequency of raw data in Hz
        :param threshold_sigma: threshold for spike detection in units of
               standard deviation
        :param filter_spec: dictionary of filter parameters
        :param filter_step_size_s: size of chunks to filter in seconds
        '''
        # Filter the data.
        if do_filter:
            data = filter(raw_data, fs_Hz, filter_order, filter_lo_Hz,
                          filter_hi_Hz, time_step_size_s)
        else:
            # This is bad form
            data = raw_data

        threshold = threshold_sigma * np.std(data, axis=1, keepdims=True)

        if direction == 'both':
            raster = (data > threshold) | (data < -threshold)
        elif direction == 'up':
            raster = data > threshold
        elif direction == 'down':
            raster = data < -threshold

        if hysteresis:
            raster = np.diff(np.array(raster, dtype=int), axis=1) == 1

        self.idces, t_idces = np.nonzero(raster)

        self.times_ms = t_idces / fs_Hz * 1000

        self.N = data.shape[0]
        fs_ms = fs_Hz / 1000
        self.length = data.shape[1] / fs_ms

        # If no spikes were found, we can't do anything else.
        if len(self.idces) == 0:
            self.has_spikes = False
        else:
            self.has_spikes = True

        # change this to be an instance of the parent class instead
        # super().__init__(idces, times_ms, **kwargs)

    def to_spikeData(self, N=None, length=None):
        if self.has_spikes:
            if N is None:
                N = self.N
            if length is None:
                length = self.length
            return SpikeData(self.idces, self.times_ms, N=N, length=length)
        else:
            print('No spikes found.')
            return None
