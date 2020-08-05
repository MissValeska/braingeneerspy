import numpy as np

class Neurons():
    """
    Base class for a culture of neurons. New neuronal models should
    only require modifying the constructor and the _step() method.
    """
    def __init__(self, N, dt):
        self.N = N
        self.dt = dt
        self.input_synapses = []
        self.reset()

    def reset(self):
        """
        Reset the states of all the neurons to their resting value.
        """
        self.fired = np.zeros(self.N, dtype=np.bool)
        for syn in self.input_synapses:
            syn.reset()

    def Isyn(self):
        """
        Compute the total input to this culture from all of its
        synaptic predecessors.
        """
        Iin = np.zeros(self.N)
        for syn in self.input_synapses:
            Iin += syn.output()
        return Iin

    def list_firings(self, Iin, time):
        """
        Simulate the network for some amount of time. The return value
        is a list of pairs (time,index) indicating which cells fired
        when. These should be sorted by time.
        """
        events = []
        n_steps = int(np.ceil(time / self.dt))
        for step in range(n_steps):
            self.fired = self._step(Iin + self.Isyn())
            for idx in np.arange(self.N)[self.fired]:
                events.append((step*self.dt, idx))
            for syn in self.input_synapses:
                syn._step()
        return events

    def _step(self, Iin):
        """
        Simulate the neural culture forward one step.
        """
        raise NotImplementedError

    def spike_raster(self, Iin, time, bin_size):
        """
        Simulate the network for a fixed total time with a constant
        input parameter and return a spike raster: a matrix with
        dimensions (time, cell index) which contains True if a cell
        fired at that time.
        """
        n_bins = int(np.ceil(time / bin_size))
        raster = np.zeros((self.N, n_bins), dtype=np.bool)

        for t,i in self.list_firings(Iin, time):
            raster[i, int(t//bin_size)] = True
        return raster

    def total_firings(self, Iin, total_time):
        """
        Simulate the network for a fixed total time with a constant
        input parameter and return the total number of times each cell
        fired during that time.
        """
        return self.spike_raster(Iin, total_time, self.dt).sum(1)


class AggregateCulture(Neurons):
    """
    The basic disjoint union operation on neuronal cultures: collects
    multiple different groups of cells into an aggregate that can be
    simulated in an order-independent manner.
    """
    def __init__(self, *cultures):
        self.cultures = cultures
        N = sum(c.N for c in cultures)

        dt = cultures[0].dt
        assert all(c.dt == dt for c in cultures)

        super().__init__(N, dt)

    def Isyn(self):
        return np.hstack([c.Isyn() for c in self.cultures])

    def _step(self, Iin):
        # For each culture, update the corresponding part of the
        # firings array. I can't decide if this method is gnarly or
        # neat, but it does seem simpler than any way I could think of
        # where the slices or indices were generated in advance.
        idces = slice(0,0)
        for c in self.cultures:
            idces = slice(idces.start, idces.stop + c.N)
            c.fired = self.fired[idces] = c._step(Iin[idces])
            for syn in c.input_synapses:
                syn._step()
            idces = slice(idces.start + c.N, idces.stop)

        return self.fired


class PoissonNeurons(Neurons):
    """
    Simple Poisson stochastic neurons: the input parameter is
    interpreted as the average firing rate of the cell. There is no
    refractory period, so the observed firing rate will decrease with
    increasing dt, only reaching the requested rate in the zero limit.
    """
    def _step(self, rates):
        return np.random.poisson(rates*self.dt, self.N) > 0


class LIFNeurons(Neurons):
    """
    Leaky Integrate-and-Fire neuron model: the input value is
    interpreted as a rate of change in membrane voltage, which is
    integrated with an exponential leak (towards a resting value Vr)
    determined by the parameter tau. When V reaches Vp, it is reset
    automatically to Vr. Also, during the refractory time t_refrac
    after each firing, the cell does not respond to input.
    """
    def __init__(self, N, dt, *, Vr, Vp, tau, c=None, t_refrac=0):
        self.Vr = Vr
        self.c = np.ones(N) * (Vr if c is None else c)
        self.Vp = Vp
        self.tau = tau
        self.t_refrac = t_refrac
        super().__init__(N, dt)

    def reset(self):
        self.V = np.ones(self.N) * self.Vr
        self.timer = np.zeros(self.N)
        super().reset()

    def _step(self, Iin):
        # Do the resets AFTER the voltages have been returned because
        # plots etc will turn out nicer.
        self.V[self.fired] = self.c[self.fired]
        self.timer[self.fired] = self.t_refrac

        # Now integrate, using the midpoint method to make the
        # exponential work better maybe?
        dVdt = Iin - (self.V - self.Vr)/self.tau
        V_test = self.V + self.dt*dVdt
        dVdt = Iin - (V_test - self.Vr)/self.tau
        self.V[self.timer <= 0] += dVdt[self.timer <= 0] * self.dt
        self.timer -= self.dt

        # And fire! :)
        return self.V >= self.Vp


class IzhikevichNeurons(Neurons):
    """
    The Izhikevich neuron model as presented in Dynamical Systems in
    Neuroscience (2003). In brief, it is an adaptive quadratic
    integrate-and-fire neuron, whose phase variables v and u represent
    the membrane voltage and a membrane leakage current.

    The individual neuron model takes the following parameters; the
    book provides values matching physiological cell types.
     a : 1/ms time constant of recovery current
     b : nS steady-state conductance for recovery current
     c : mV membrane voltage after a downstroke
     d : pA bump to recovery current after a downstroke
     C : pF membrane capacitance
     k : nS/mV voltage-gated Na+ channel conductance
     Vr: mV resting membrane voltage when u=0
     Vt: mV threshold voltage when u=0
     Vp: mV action potential peak, after which reset happens
    """
    def __init__(self, N, dt, *, a, b, c, d, C, k, Vr, Vt, Vp):
        self.a = a
        self.b = b
        self.c = c * np.ones(N)
        self.d = d * np.ones(N)
        self.C = C
        self.k = k
        self.Vr = Vr
        self.Vt = Vt
        self.Vp = Vp
        super().__init__(N, dt)

    def reset(self):
        self.VU = np.vstack((self.Vr * np.ones(self.N),
                             np.zeros(self.N)))
        super().reset()

    def _vudot(self, Iin):
        return self._vudot_at(Iin, self.VU)

    def _vudot_at(self, Iin, VU):
        VUdot = np.zeros((2, self.N))
        NAcurrent = self.k*(VU[0,:] - self.Vr)*(VU[0,:] - self.Vt)
        VUdot[0,:] = (NAcurrent - VU[1,:] + Iin) / self.C
        VUdot[1,:] = self.a * (self.b*(VU[0,:] - self.Vr) - VU[1,:])
        return VUdot

    def _step(self, Iin):
        self.V[self.fired] = self.c[self.fired]
        self.U[self.fired] += self.d[self.fired]

        VU_mid = self.VU + self._vudot(Iin)*self.dt/2
        self.VU += self._vudot_at(Iin, VU_mid)*self.dt

        return self.V >= self.Vp

    @property
    def V(self):
        return self.VU[0,:]
    @V.setter
    def V(self, V):
        self.VU[0,:] = V

    @property
    def U(self):
        return self.VU[1,:]
    @U.setter
    def U(self, U):
        self.VU[1,:] = U


class Synapses():
    """
    Base class for a group of synaptic connections between two neural
    cultures `inputs` and `outputs`.
    """
    def __init__(self, inputs, outputs=None):
        self.inputs = inputs
        if outputs is None:
            self.outputs = inputs
        else:
            self.outputs = outputs
            assert inputs.dt == outputs.dt, \
                'Synapses can only connect cultures with identical dt.'
        self.M = self.inputs.N
        self.N = self.outputs.N
        self.dt = self.inputs.dt
        self.outputs.input_synapses.append(self)
        self.reset()

    def output(self):
        """
        Return the numerical input that these synapses should be
        providing to the postsynaptic cells, given the current state
        of the presynaptic cells.
        """
        raise NotImplementedError

    def _step(self):
        """
        If these synapses have intrinsic dynamics, advance them one
        timestep, returning nothing.
        """
        pass

    def reset(self):
        """
        Reset the state variables of the synapses to their resting
        values, to support resetting of neural cultures.
        """
        raise NotImplementedError


class ExponentialSynapses(Synapses):
    """
    A synaptic connection block where each presynaptic firing creates
    an exponentially-decaying synaptic conductance. To simplify
    things, the synaptic reversal potential is specified for an entire
    synapse group rather than per-presynaptic-neuron.

    Additionally, stochastic activity is supported by passing the
    parameters noise_event_rate and noise_event_size. These determine
    the frequency and magnitude of spontaneous synaptic activations.
    """
    def __init__(self, inputs, outputs=None, *, tau, G, Vn,
                 noise_event_rate=0, noise_event_size=0.1):
        super().__init__(inputs, outputs)
        self.G = np.asarray(G)
        assert self.G.shape == (self.N, self.M), \
            f'Synaptic matrix should be (N,M), but is {self.G.shape}'
        self.tau = tau
        self.Vn = Vn
        self.noise_event_rate = noise_event_rate
        self.noise_event_size = noise_event_size

    def output(self):
        return (self.G@self.a) * (self.Vn - self.outputs.V)

    def _step(self):
        self.a[self.inputs.fired] += 1
        # TODO: this noise is correlated between postsynaptic cells,
        # but creating noise per-synapse is horribly inefficient.
        # It's probably best to compute a normal approximation to
        # a sum of Poisson RVs.
        num_events = np.random.poisson(size=self.M,
                                       lam=self.dt*self.noise_event_rate)
        self.a += self.noise_event_size * num_events
        self.a -= self.dt/self.tau * self.a

    def reset(self):
        self.a = np.zeros(self.M)


class DiehlCook2015(AggregateCulture):
    def __init__(self, N, dt):
        # Populations of input, excitatory, and inhibitory neurons.
        self.input_layer = PoissonNeurons(784, dt)
        self.exc = LIFNeurons(N, dt, Vr=-65, c=-65, Vp=-52,
                              tau=100, t_refrac=5)
        self.inh = LIFNeurons(N, dt, Vr=-60, c=-45, Vp=-40,
                              tau=10, t_refrac=2)

        # Input->excitatory synapses.
        ExponentialSynapses(self.input_layer, self.exc,
                            tau=1, Vn=0,
                            G=0.05*np.random.rand(N,784)
                            *(np.random.rand(N,784) < 0.1))
        # Excitatory->inhibitory synapses.
        ExponentialSynapses(self.exc, self.inh,
                            tau=1, Vn=0,
                            G=1*np.eye(N))
        # Synapses for lateral inhibition.
        ExponentialSynapses(self.inh, self.exc,
                            tau=2, Vn=-100,
                            G=1*(1-np.eye(N)))

        super().__init__(self.input_layer, self.exc, self.inh)

    def list_firings(self, Iin, time):
        """
        Simulate the network for some amount of time. This network
        only accepts a reduced version of the typical Iin to set the
        rates of the Poisson input layer.
        """
        Iin_full = np.zeros(self.N)
        Iin_full[:self.input_layer.N] = np.asarray(Iin).flatten()
        return super().list_firings(Iin_full, time)

    def present(self, digit, off_time=150, on_time=350):
        """
        Present a digit to the network: first, allow the network to
        relax with zero input, then use the input digit as the rate
        argument to the input layer, and return the results.
        """
        self.total_firings(np.zeros(self.input_layer.N), off_time)

        # Flatten the input digit and provide it to the input layer,
        # but also include zeros to send to the rest of the neurons.
        return self.total_firings(digit, on_time)[784:784+self.exc.N]
