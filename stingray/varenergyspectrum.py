import numpy as np
import warnings
from stingray.base import StingrayObject
from stingray.gti import check_separate, cross_two_gtis

from stingray.lightcurve import Lightcurve
from stingray.utils import assign_value_if_none, simon, excess_variance, show_progress

from stingray.fourier import avg_cs_from_events, avg_pds_from_events, fftfreq, get_average_ctrate
from stingray.fourier import poisson_level, error_on_averaged_cross_spectrum, cross_to_covariance
from abc import ABCMeta, abstractmethod


__all__ = [
    "VarEnergySpectrum",
    "RmsEnergySpectrum",
    "RmsSpectrum",
    "LagEnergySpectrum",
    "LagSpectrum",
    "ExcessVarianceSpectrum",
    "CovarianceSpectrum",
    "ComplexCovarianceSpectrum",
    "CountSpectrum",
]


def get_non_overlapping_ref_band(channel_band, ref_band):
    """
    Ensures that the ``channel_band`` (i.e. the band of interest) is
    not contained within the ``ref_band`` (i.e. the reference band)

    Parameters
    ----------
    channel_band : iterable of type ``[elow, ehigh]``
        The lower/upper limits of the energies to be contained in the band
        of interest

    ref_band : iterable
        The lower/upper limits of the energies in the reference band

    Returns
    -------
    ref_intervals : iterable
        The channels that are both in the reference band in not in the
        bands of interest

    Examples
    --------
    >>> channel_band = [2, 3]
    >>> ref_band = [[0, 10]]
    >>> new_ref = get_non_overlapping_ref_band(channel_band, ref_band)
    >>> np.allclose(new_ref, [[0, 2], [3, 10]])
    True

    Test this also works with a 1-D ref. band
    >>> new_ref = get_non_overlapping_ref_band(channel_band, [0, 10])
    >>> np.allclose(new_ref, [[0, 2], [3, 10]])
    True
    >>> new_ref = get_non_overlapping_ref_band([0, 1], [[2, 3]])
    >>> np.allclose(new_ref, [[2, 3]])
    True
    """
    channel_band = np.asarray(channel_band)
    ref_band = np.asarray(ref_band)
    if len(ref_band.shape) <= 1:
        ref_band = np.asarray([ref_band])
    if check_separate(ref_band, [channel_band]):
        return np.asarray(ref_band)
    not_channel_band = [
        [0, channel_band[0]],
        [channel_band[1], np.max([np.max(ref_band), channel_band[1] + 1])],
    ]

    return cross_two_gtis(ref_band, not_channel_band)


def _decode_energy_specification(energy_spec):
    """Decode the energy specification tuple.

    Parameters
    ----------
    energy_spec : iterable
        list containing the energy specification
        Must have the following structure:
            * energy_spec[0]: lower edge of (log) energy space
            * energy_spec[1]: upper edge of (log) energy space
            * energy_spec[2] +1 : energy bin edges (hence the +1)
            * {`lin` | `log`} flat deciding whether the energy space is linear
              or logarithmic

    Returns
    -------
    energies : numpy.ndarray
        An array of lower/upper bin edges for the energy array

    Examples
    --------
    >>> _decode_energy_specification([0, 2, 2, 'lin'])
    Traceback (most recent call last):
     ...
    ValueError: Energy specification must be a tuple
    >>> a = _decode_energy_specification((0, 2, 2, 'lin'))
    >>> np.allclose(a, [0, 1, 2])
    True
    >>> a = _decode_energy_specification((1, 4, 2, 'log'))
    >>> np.allclose(a, [1, 2, 4])
    True
    """
    if not isinstance(energy_spec, tuple):
        raise ValueError("Energy specification must be a tuple")

    if energy_spec[-1].lower() not in ["lin", "log"]:
        raise ValueError("Incorrect energy specification")

    log_distr = True if energy_spec[-1].lower() == "log" else False

    if log_distr:
        energies = np.logspace(
            np.log10(energy_spec[0]), np.log10(energy_spec[1]), energy_spec[2] + 1
        )
    else:
        energies = np.linspace(energy_spec[0], energy_spec[1], energy_spec[2] + 1)

    return energies


class VarEnergySpectrum(StingrayObject, metaclass=ABCMeta):
    main_array_attr = "energy"
    """
    Base class for variability-energy spectrum.

    This class is only a base for the various variability spectra, and it's
    not to be instantiated by itself.

    Parameters
    ----------
    events : :class:`stingray.events.EventList` object
        event list

    freq_interval : ``[f0, f1]``, floats
        the frequency range over which calculating the variability quantity

    energy_spec : list or tuple ``(emin, emax, N, type)``
        if a ``list`` is specified, this is interpreted as a list of bin edges;
        if a ``tuple`` is provided, this will encode the minimum and maximum
        energies, the number of intervals, and ``lin`` or ``log``.

    Other Parameters
    ----------------
    ref_band : ``[emin, emax``], floats; default ``None``
        minimum and maximum energy of the reference band. If ``None``, the
        full band is used.

    use_pi : bool, default ``False``
        Use channel instead of energy

    events2 : :class:`stingray.events.EventList` object
        event list for the second channel, if not the same. Useful if the
        reference band has to be taken from another detector.

    return_complex: bool, default False
        In spectra that produce complex values, return the whole spectrum.
        Otherwise, the absolute value will be returned.

    Attributes
    ----------
    events1 : array-like
        list of events used to produce the spectrum

    events2 : array-like
        if the spectrum requires it, second list of events

    freq_interval : array-like
        interval of frequencies used to calculate the spectrum

    energy_intervals : ``[[e00, e01], [e10, e11], ...]``
        energy intervals used for the spectrum

    spectrum : array-like
        the spectral values, corresponding to each energy interval

    spectrum_error : array-like
        the error bars corresponding to spectrum

    energy : array-like
        The centers of energy intervals
    """

    def __init__(
        self,
        events,
        freq_interval,
        energy_spec,
        ref_band=None,
        bin_time=1,
        use_pi=False,
        segment_size=None,
        events2=None,
        return_complex=False,
    ):
        self.events1 = events
        self.events2 = assign_value_if_none(events2, events)
        self._analyze_inputs()
        # This will be set to True in ComplexCovariance
        self.return_complex = return_complex

        self.freq_interval = freq_interval
        self.use_pi = use_pi
        self.bin_time = bin_time

        if isinstance(energy_spec, tuple):
            energies = _decode_energy_specification(energy_spec)
        else:
            energies = np.asarray(energy_spec)

        self.energy_intervals = list(zip(energies[0:-1], energies[1:]))

        self.ref_band = np.asarray(assign_value_if_none(ref_band, [0, np.inf]))

        if len(self.ref_band.shape) <= 1:
            self.ref_band = np.asarray([self.ref_band])

        self.segment_size = self.delta_nu = None
        if segment_size is not None:
            self.segment_size = segment_size
            self.delta_nu = 1 / self.segment_size

        self._create_empty_spectrum()

        if len(events.time) == 0:
            simon("There are no events in your event list!" + "Can't make a spectrum!")
        else:
            self._spectrum_function()

    @property
    def energy(self):
        """Give the centers of the energy intervals."""
        return np.sum(self.energy_intervals, axis=1) / 2

    def _analyze_inputs(self):
        """Make some checks on the inputs and set some internal variable.

        If the object of events1 is the same as events2, set `same_events` to True.
        This will, for example, tell the methods to use events1 for the subject bands
        and events2 for the reference band (useful in deadtime-affected data).

        Also, if the event lists are distinct, calculate common GTIs.
        """
        events1 = self.events1
        events2 = self.events2
        common_gti = events1.gti
        if events2 is None or events2 is events1:
            self.events2 = self.events1
            self.same_events = True
        else:
            common_gti = cross_two_gtis(events1.gti, events2.gti)
            self.same_events = False
        self.gti = common_gti

    def _create_empty_spectrum(self):
        """Allocate the arrays of the output spectrum.

        Default value is NaN. This is because most spectral timing products are
        prone to numerical errors, and it's more informative to have a default invalid
        value rather than something like, e.g., 0 or 1
        """
        if self.return_complex:
            dtype = complex
        else:
            dtype = float

        self.spectrum = np.zeros(len(self.energy_intervals), dtype=dtype) + np.nan
        self.spectrum_error = np.zeros_like(self.spectrum, dtype=dtype) + np.nan

    def _get_times_from_energy_range(self, events, erange, use_pi=False):
        """Get event times from the wanted energy range.

        Parameters
        ----------
        events : `EventList`
            Input event list
        erange : [e0, e1]
            Energy range in keV

        Other parameters
        ----------------
        use_pi : bool, default False
            Use the PI channel instead of energies

        Returns
        -------
        out_ev : `EventList`
            The filtered event list.
        """
        if use_pi:
            energies = events.pi
        else:
            energies = events.energy
        mask = (energies >= erange[0]) & (energies < erange[1])
        return events.time[mask]

    def _get_good_frequency_bins(self, freq=None):
        """Get frequency mask corresponding to the wanted frequency interval

        Parameters
        ----------
        freq : `np.array`, default None
            The frequency array. If None, it will get calculated from the number
            of spectral bins using `np.fft.fftfreq`

        Returns
        -------
        freq_mask : `np.array` of bool
            The frequency mask.
        """
        if freq is None:
            n_bin = np.rint(self.segment_size / self.bin_time)
            freq = fftfreq(int(n_bin), self.bin_time)
            freq = freq[freq > 0]
        good = (freq >= self.freq_interval[0]) & (freq < self.freq_interval[1])
        return good

    def _construct_lightcurves(
        self, channel_band, tstart=None, tstop=None, exclude=True, only_base=False
    ):
        """
        Construct light curves from event data, for each band of interest.

        Parameters
        ----------
        channel_band : iterable of type ``[elow, ehigh]``
            The lower/upper limits of the energies to be contained in the band
            of interest

        tstart : float, optional, default ``None``
            A common start time (if start of observation is different from
            the first recorded event)

        tstop : float, optional, default ``None``
            A common stop time (if start of observation is different from
            the first recorded event)

        exclude : bool, optional, default ``True``
            if ``True``, exclude the band of interest from the reference band

        only_base : bool, optional, default ``False``
            if ``True``, only return the light curve of the channel of interest, not
            that of the reference band

        Returns
        -------
        base_lc : :class:`Lightcurve` object
            The light curve of the channels of interest

        ref_lc : :class:`Lightcurve` object (only returned if ``only_base`` is ``False``)
            The reference light curve for comparison with ``base_lc``
        """
        if self.use_pi:
            energies1 = self.events1.pi
            energies2 = self.events2.pi
        else:
            energies2 = self.events2.energy
            energies1 = self.events1.energy

        gti = cross_two_gtis(self.events1.gti, self.events2.gti)

        tstart = assign_value_if_none(tstart, gti[0, 0])
        tstop = assign_value_if_none(tstop, gti[-1, -1])

        good = (energies1 >= channel_band[0]) & (energies1 < channel_band[1])
        base_lc = Lightcurve.make_lightcurve(
            self.events1.time[good],
            self.bin_time,
            tstart=tstart,
            tseg=tstop - tstart,
            gti=gti,
            mjdref=self.events1.mjdref,
        )

        if only_base:
            return base_lc

        if exclude:
            ref_intervals = get_non_overlapping_ref_band(channel_band, self.ref_band)
        else:
            ref_intervals = self.ref_band

        ref_lc = Lightcurve(
            base_lc.time,
            np.zeros_like(base_lc.counts),
            gti=base_lc.gti,
            mjdref=base_lc.mjdref,
            dt=base_lc.dt,
            err_dist=base_lc.err_dist,
            skip_checks=True,
        )

        for i in ref_intervals:
            good = (energies2 >= i[0]) & (energies2 < i[1])
            new_lc = Lightcurve.make_lightcurve(
                self.events2.time[good],
                self.bin_time,
                tstart=tstart,
                tseg=tstop - tstart,
                gti=base_lc.gti,
                mjdref=self.events2.mjdref,
            )
            ref_lc = ref_lc + new_lc

        ref_lc.err_dist = base_lc.err_dist
        return base_lc, ref_lc

    @abstractmethod
    def _spectrum_function(self):
        pass

    def from_astropy_table(self, *args, **kwargs):
        raise NotImplementedError(
            "from_XXXX methods are not implemented for VarEnergySpectrum")

    def from_xarray(self, *args, **kwargs):
        raise NotImplementedError(
            "from_XXXX methods are not implemented for VarEnergySpectrum")

    def from_pandas(self, *args, **kwargs):
        raise NotImplementedError(
            "from_XXXX methods are not implemented for VarEnergySpectrum")


class RmsSpectrum(VarEnergySpectrum):
    """Calculate the rms-Energy spectrum.

    For each energy interval, calculate the power density spectrum in
    absolute or fractional r.m.s. normalization, and integrate it in the
    given frequency range to obtain the rms. If ``events2`` is specified,
    the cospectrum is used instead of the PDS.

    We assume absolute r.m.s. normalization. To get the fractional r.m.s.
    we just divide by the mean count rate.

    Parameters
    ----------
    events : :class:`stingray.events.EventList` object
        event list

    freq_interval : ``[f0, f1]``, list of float
        the frequency range over which calculating the variability quantity

    energy_spec : list or tuple ``(emin, emax, N, type)``
        if a ``list`` is specified, this is interpreted as a list of bin edges;
        if a ``tuple`` is provided, this will encode the minimum and maximum
        energies, the number of intervals, and ``lin`` or ``log``.

    Other Parameters
    ----------------
    ref_band : ``[emin, emax]``, float; default ``None``
        minimum and maximum energy of the reference band. If ``None``, the
        full band is used.

    use_pi : bool, default ``False``
        Use channel instead of energy

    events2 : :class:`stingray.events.EventList` object
        event list for the second channel, if not the same. Useful if the
        reference band has to be taken from another detector.

    norm : str, one of ["abs", "frac"]
        The normalization of the rms, whether absolute or fractional.

    Attributes
    ----------
    events1 : array-like
        list of events used to produce the spectrum

    events2 : array-like
        if the spectrum requires it, second list of events

    freq_interval : array-like
        interval of frequencies used to calculate the spectrum

    energy_intervals : ``[[e00, e01], [e10, e11], ...]``
        energy intervals used for the spectrum

    spectrum : array-like
        the spectral values, corresponding to each energy interval

    spectrum_error : array-like
        the errorbars corresponding to spectrum
    """

    def __init__(
        self,
        events,
        energy_spec,
        ref_band=None,
        freq_interval=[0, 1],
        bin_time=1,
        use_pi=False,
        segment_size=None,
        events2=None,
        norm="frac",
    ):

        self.norm = norm
        VarEnergySpectrum.__init__(
            self,
            events,
            freq_interval=freq_interval,
            energy_spec=energy_spec,
            bin_time=bin_time,
            use_pi=use_pi,
            ref_band=ref_band,
            segment_size=segment_size,
            events2=events2,
        )

    def _spectrum_function(self):

        # Get the frequency bins to be averaged in the final results.
        good = self._get_good_frequency_bins()
        n_ave_bin = np.count_nonzero(good)

        # Get the frequency resolution of the final spectrum.
        delta_nu_after_mean = self.delta_nu * n_ave_bin

        for i, eint in enumerate(show_progress(self.energy_intervals)):
            # Extract events from the subject band and calculate the count rate
            # and Poisson noise level.
            sub_events = self._get_times_from_energy_range(self.events1, eint)
            countrate_sub = get_average_ctrate(sub_events, self.gti, self.segment_size)
            sub_power_noise = poisson_level(norm="abs", meanrate=countrate_sub)

            # If we provided the `events2` array, calculate the rms from the
            # cospectrum, otherwise from the PDS
            if not self.same_events:
                # Extract events from the subject band in the other array, and
                # calculate the count rate and Poisson noise level.
                sub_events2 = self._get_times_from_energy_range(self.events2, eint)
                countrate_sub2 = get_average_ctrate(sub_events2, self.gti, self.segment_size)
                sub2_power_noise = poisson_level(norm="abs", meanrate=countrate_sub2)

                # Calculate the cross spectrum
                results = avg_cs_from_events(
                    sub_events,
                    sub_events2,
                    self.gti,
                    self.segment_size,
                    self.bin_time,
                    silent=True,
                    norm="abs",
                )
                if results is None:
                    continue
                cross = results["power"]

                m_ave, mean = [results.meta[key] for key in ["m", "mean"]]
                mean_power = np.mean(cross[good])
                power_noise = 0
                rmsnoise = np.sqrt(delta_nu_after_mean *
                                   np.sqrt(sub_power_noise * sub2_power_noise))
            else:
                results = avg_pds_from_events(
                    sub_events, self.gti, self.segment_size, self.bin_time, silent=True, norm="abs"
                )
                if results is None:
                    continue
                sub_power = results["power"]
                m_ave, mean = [results.meta[key] for key in ["m", "mean"]]

                mean_power = np.mean(sub_power[good])
                power_noise = sub_power_noise
                rmsnoise = np.sqrt(delta_nu_after_mean * power_noise)

            meanrate = mean / self.bin_time

            rms = np.sqrt(np.abs(mean_power - power_noise) * delta_nu_after_mean)

            # Assume coherence 0, use Ingram+2019
            num = rms ** 4 + rmsnoise ** 4 + 2 * rms * rmsnoise
            den = 4 * m_ave * n_ave_bin * rms ** 2

            rms_err = np.sqrt(num / den)
            if self.norm == "frac":
                rms, rms_err = rms / meanrate, rms_err / meanrate

            self.spectrum[i] = rms
            self.spectrum_error[i] = rms_err


RmsEnergySpectrum = RmsSpectrum


class ExcessVarianceSpectrum(VarEnergySpectrum):
    """Calculate the Excess Variance spectrum.

    For each energy interval, calculate the excess variance in the specified
    frequency range.

    Parameters
    ----------
    events : :class:`stingray.events.EventList` object
        event list

    freq_interval : ``[f0, f1]``, list of float
        the frequency range over which calculating the variability quantity

    energy_spec : list or tuple ``(emin, emax, N, type)``
        if a list is specified, this is interpreted as a list of bin edges;
        if a tuple is provided, this will encode the minimum and maximum
        energies, the number of intervals, and ``lin`` or ``log``.

    Other Parameters
    ----------------
    ref_band : ``[emin, emax]``, floats; default ``None``
        minimum and maximum energy of the reference band. If ``None``, the
        full band is used.

    use_pi : bool, default ``False``
        Use channel instead of energy

    Attributes
    ----------
    events1 : array-like
        list of events used to produce the spectrum

    freq_interval : array-like
        interval of frequencies used to calculate the spectrum

    energy_intervals : ``[[e00, e01], [e10, e11], ...]``
        energy intervals used for the spectrum

    spectrum : array-like
        the spectral values, corresponding to each energy interval

    spectrum_error : array-like
        the errorbars corresponding to spectrum
    """

    def __init__(
        self,
        events,
        freq_interval,
        energy_spec,
        bin_time=1,
        use_pi=False,
        segment_size=None,
        normalization="fvar",
    ):

        self.normalization = normalization
        accepted_normalizations = ["fvar", "none"]
        if normalization not in accepted_normalizations:
            raise ValueError(
                "The normalization of excess variance must be "
                "one of {}".format(accepted_normalizations)
            )

        VarEnergySpectrum.__init__(
            self,
            events,
            freq_interval,
            energy_spec,
            bin_time=bin_time,
            use_pi=use_pi,
            segment_size=segment_size,
        )

    def _spectrum_function(self):
        spec = np.zeros(len(self.energy_intervals))
        spec_err = np.zeros_like(spec)
        for i, eint in enumerate(self.energy_intervals):
            lc = self._construct_lightcurves(eint, exclude=False, only_base=True)

            spec[i], spec_err[i] = excess_variance(lc, self.normalization)

        return spec, spec_err


class CountSpectrum(VarEnergySpectrum):
    """Calculate the energy spectrum.

    For each energy interval, compute the counts.

    Parameters
    ----------
    events : :class:`stingray.events.EventList` object
        event list

    energy_spec : list or tuple ``(emin, emax, N, type)``
        if a ``list`` is specified, this is interpreted as a list of bin edges;
        if a ``tuple`` is provided, this will encode the minimum and maximum
        energies, the number of intervals, and ``lin`` or ``log``.

    Other Parameters
    ----------------
    use_pi : bool, default ``False``
        Use channel instead of energy

    Attributes
    ----------
    events1 : array-like
        list of events used to produce the spectrum

    energy_intervals : ``[[e00, e01], [e10, e11], ...]``
        energy intervals used for the spectrum

    spectrum : array-like
        the spectral values, corresponding to each energy interval

    spectrum_error : array-like
        the errorbars corresponding to spectrum
    """

    def __init__(
        self,
        events,
        energy_spec,
        use_pi=False
    ):

        VarEnergySpectrum.__init__(
            self,
            events,
            None,
            energy_spec,
            use_pi=use_pi,
        )

    def _spectrum_function(self):
        events = self.events1

        for i, eint in show_progress(enumerate(self.energy_intervals)):
            sub_events = self._get_times_from_energy_range(events, eint, use_pi=self.use_pi)

            sp = sub_events.size
            self.spectrum[i] = sp
            self.spectrum_error[i] = np.sqrt(sp)


class LagSpectrum(VarEnergySpectrum):
    """Calculate the lag-energy spectrum.

    For each energy interval, calculate the lag between two bands.
    If ``events2`` is specified, the energy bands are chosen from this second
    event list, while the reference band from ``events``.

    Parameters
    ----------
    events : :class:`stingray.events.EventList` object
        event list

    freq_interval : ``[f0, f1]``, list of float
        the frequency range over which calculating the variability quantity

    energy_spec : list or tuple ``(emin, emax, N, type)``
        if a ``list`` is specified, this is interpreted as a list of bin edges;
        if a ``tuple`` is provided, this will encode the minimum and maximum
        energies, the number of intervals, and ``lin`` or ``log``.

    Other Parameters
    ----------------
    ref_band : ``[emin, emax]``, float; default ``None``
        minimum and maximum energy of the reference band. If ``None``, the
        full band is used.

    use_pi : bool, default ``False``
        Use channel instead of energy

    events2 : :class:`stingray.events.EventList` object
        event list for the second channel, if not the same. Useful if the
        reference band has to be taken from another detector.

    Attributes
    ----------
    events1 : array-like
        list of events used to produce the spectrum

    events2 : array-like
        if the spectrum requires it, second list of events

    freq_interval : array-like
        interval of frequencies used to calculate the spectrum

    energy_intervals : ``[[e00, e01], [e10, e11], ...]``
        energy intervals used for the spectrum

    spectrum : array-like
        the lag values, corresponding to each energy interval

    spectrum_error : array-like
        the errorbars corresponding to spectrum
    """

    # events, freq_interval, energy_spec, ref_band = None
    def __init__(
        self,
        events,
        freq_interval,
        energy_spec,
        ref_band=None,
        bin_time=1,
        use_pi=False,
        segment_size=None,
        events2=None,
    ):

        VarEnergySpectrum.__init__(
            self,
            events,
            freq_interval,
            energy_spec=energy_spec,
            bin_time=bin_time,
            use_pi=use_pi,
            ref_band=ref_band,
            segment_size=segment_size,
            events2=events2,
        )

    def _spectrum_function(self):
        # Extract the photon arrival times from the reference band
        ref_events = self._get_times_from_energy_range(self.events2, self.ref_band[0])
        ref_power_noise = poisson_level(norm="none", n_ph=ref_events.size)

        # Calculate the PDS in the reference band. Needed to calculate errors.
        results = avg_pds_from_events(
            ref_events, self.gti, self.segment_size, self.bin_time, silent=True, norm="none"
        )
        freq = results["freq"]
        ref_power = results["power"]
        m_ave = results.meta["m"]

        # Get the frequency bins to be averaged in the final results.
        good = self._get_good_frequency_bins(freq)
        mean_ref_power = np.mean(ref_power[good])
        n_ave_bin = np.count_nonzero(good)

        m_tot = n_ave_bin * m_ave

        f = (self.freq_interval[0] + self.freq_interval[1]) / 2
        for i, eint in enumerate(show_progress(self.energy_intervals)):
            # Extract the photon arrival times from the subject band
            sub_events = self._get_times_from_energy_range(self.events1, eint)
            sub_power_noise = poisson_level(norm="none", n_ph=sub_events.size)

            results_cross = avg_cs_from_events(
                sub_events,
                ref_events,
                self.gti,
                self.segment_size,
                self.bin_time,
                silent=True,
                norm="none"
            )

            results_ps = avg_pds_from_events(
                sub_events, self.gti, self.segment_size, self.bin_time, silent=True, norm="none"
            )

            if results_cross is None or results_ps is None:
                continue

            cross = results_cross["power"]
            sub_power = results_ps["power"]

            Cmean = np.mean(cross[good])

            mean_sub_power = np.mean(sub_power[good])

            # Is the subject band overlapping with the reference band?
            # This will be used to correct the error bars, following
            # Ingram 2019.
            common_ref = self.same_events and len(cross_two_gtis([eint], self.ref_band)) > 0

            _, _, phi_e, _ = error_on_averaged_cross_spectrum(
                Cmean, mean_sub_power, mean_ref_power, m_tot, sub_power_noise, ref_power_noise, common_ref=common_ref
            )

            lag = np.mean((np.angle(cross[good]) / (2 * np.pi * freq[good])))

            lag_e = phi_e / (2 * np.pi * f)
            self.spectrum[i] = lag
            self.spectrum_error[i] = lag_e


LagEnergySpectrum = LagSpectrum


class ComplexCovarianceSpectrum(VarEnergySpectrum):
    """Calculate the complex covariance spectrum.

    For each energy interval, calculate the covariance between two bands.
    If ``events2`` is specified, the energy bands are chosen from this second
    event list, while the reference band from ``events``.

    Mastroserio et al. 2018, MNRAS, 475, 4027

    We assume absolute r.m.s. normalization. To get the fractional r.m.s.
    we just divide by the mean count rate.

    Parameters
    ----------
    events : :class:`stingray.events.EventList` object
        event list

    freq_interval : ``[f0, f1]``, list of float
        the frequency range over which calculating the variability quantity

    energy_spec : list or tuple ``(emin, emax, N, type)``
        if a ``list`` is specified, this is interpreted as a list of bin edges;
        if a ``tuple`` is provided, this will encode the minimum and maximum
        energies, the number of intervals, and ``lin`` or ``log``.

    Other Parameters
    ----------------
    ref_band : ``[emin, emax]``, float; default ``None``
        minimum and maximum energy of the reference band. If ``None``, the
        full band is used.

    use_pi : bool, default ``False``
        Use channel instead of energy

    events2 : :class:`stingray.events.EventList` object
        event list for the second channel, if not the same. Useful if the
        reference band has to be taken from another detector.

    norm : str, one of ["abs", "frac"]
        The normalization of the covariance, whether absolute or fractional.

    Attributes
    ----------
    events1 : array-like
        list of events used to produce the spectrum

    events2 : array-like
        if the spectrum requires it, second list of events

    freq_interval : array-like
        interval of frequencies used to calculate the spectrum

    energy_intervals : ``[[e00, e01], [e10, e11], ...]``
        energy intervals used for the spectrum

    spectrum : array-like
        the spectral values, corresponding to each energy interval

    spectrum_error : array-like
        the errorbars corresponding to spectrum
    """

    def __init__(
        self,
        events,
        energy_spec,
        ref_band=None,
        freq_interval=[0, 1],
        bin_time=1,
        use_pi=False,
        segment_size=None,
        events2=None,
        norm="frac",
        return_complex=True,
    ):

        self.norm = norm
        VarEnergySpectrum.__init__(
            self,
            events,
            freq_interval=freq_interval,
            energy_spec=energy_spec,
            bin_time=bin_time,
            use_pi=use_pi,
            ref_band=ref_band,
            segment_size=segment_size,
            events2=events2,
            return_complex=return_complex
        )

    def _spectrum_function(self):
        # Extract events from the reference band and calculate the PDS and
        # the Poisson noise level.
        ref_events = self._get_times_from_energy_range(self.events2, self.ref_band[0])
        countrate_ref = get_average_ctrate(ref_events, self.gti, self.segment_size)
        ref_power_noise = poisson_level(norm="abs", meanrate=countrate_ref)

        results = avg_pds_from_events(
            ref_events, self.gti, self.segment_size, self.bin_time, silent=True, norm="abs"
        )
        freq = results["freq"]
        ref_power = results["power"]
        m_ave = results.meta["m"]

        # Select the frequency range to be averaged for the measurement.
        good = (freq >= self.freq_interval[0]) & (freq < self.freq_interval[1])
        n_ave_bin = np.count_nonzero(good)
        mean_ref_power = np.mean(ref_power[good])

        m_tot = m_ave * n_ave_bin
        # Frequency resolution
        delta_nu = n_ave_bin * self.delta_nu

        for i, eint in enumerate(show_progress(self.energy_intervals)):
            # Extract events from the subject band
            sub_events = self._get_times_from_energy_range(self.events1, eint)
            countrate_sub = get_average_ctrate(sub_events, self.gti, self.segment_size)
            sub_power_noise = poisson_level(norm="abs", meanrate=countrate_sub)

            results_cross = avg_cs_from_events(
                sub_events,
                ref_events,
                self.gti,
                self.segment_size,
                self.bin_time,
                silent=True,
                norm="abs",
            )

            results_ps = avg_pds_from_events(
                sub_events, self.gti, self.segment_size, self.bin_time, silent=True, norm="abs"
            )

            if results_cross is None or results_ps is None:
                continue

            cross = results_cross["power"]
            sub_power = results_ps["power"]
            mean = results_ps.meta["mean"]

            # Is the subject band overlapping with the reference band?
            # This will be used to correct the error bars, following
            # Ingram 2019.
            common_ref = self.same_events and len(cross_two_gtis([eint], self.ref_band)) > 0
            Cmean = np.mean(cross[good])
            if common_ref:
                # Equation 6 from Ingram+2019
                Cmean -= sub_power_noise

            Cmean_real = np.abs(Cmean)

            mean_sub_power = np.mean(sub_power[good])

            _, _, _, Ce = error_on_averaged_cross_spectrum(
                Cmean, mean_sub_power, mean_ref_power, m_tot, sub_power_noise, ref_power_noise, common_ref=common_ref
            )
            if not self.return_complex:
                Cmean = Cmean_real

            # Convert the cross spectrum to a covariance.
            cov, cov_e = cross_to_covariance(np.asarray(
                [Cmean, Ce]), mean_ref_power, ref_power_noise, delta_nu)

            meanrate = mean / self.bin_time

            if self.norm == "frac":
                cov, cov_e = cov / meanrate, cov_e / meanrate

            self.spectrum[i] = cov
            self.spectrum_error[i] = cov_e


class CovarianceSpectrum(ComplexCovarianceSpectrum):
    """Calculate the covariance spectrum.

    This is just the absolute value of the complex covariance
    spectrum. Refer to that documentation for details.

    For the original formulation of the covariance spectrum,
    see:
    Wilkinson & Uttley 2009, MNRAS, 397, 666

    Parameters
    ----------
    events : :class:`stingray.events.EventList` object
        event list

    freq_interval : ``[f0, f1]``, list of float
        the frequency range over which calculating the variability quantity

    energy_spec : list or tuple ``(emin, emax, N, type)``
        if a ``list`` is specified, this is interpreted as a list of bin edges;
        if a ``tuple`` is provided, this will encode the minimum and maximum
        energies, the number of intervals, and ``lin`` or ``log``.

    Other Parameters
    ----------------
    ref_band : ``[emin, emax]``, float; default ``None``
        minimum and maximum energy of the reference band. If ``None``, the
        full band is used.

    use_pi : bool, default ``False``
        Use channel instead of energy

    events2 : :class:`stingray.events.EventList` object
        event list for the second channel, if not the same. Useful if the
        reference band has to be taken from another detector.

    norm : str, one of ["abs", "frac"]
        The normalization of the covariance, whether absolute or fractional.

    Attributes
    ----------
    events1 : array-like
        list of events used to produce the spectrum

    events2 : array-like
        if the spectrum requires it, second list of events

    freq_interval : array-like
        interval of frequencies used to calculate the spectrum

    energy_intervals : ``[[e00, e01], [e10, e11], ...]``
        energy intervals used for the spectrum

    spectrum : array-like
        the spectral values, corresponding to each energy interval

    spectrum_error : array-like
        the errorbars corresponding to spectrum
    """

    def __init__(
        self,
        events,
        energy_spec,
        ref_band=None,
        freq_interval=[0, 1],
        bin_time=1,
        use_pi=False,
        segment_size=None,
        events2=None,
        norm="abs",
    ):
        ComplexCovarianceSpectrum.__init__(
            self,
            events,
            freq_interval=freq_interval,
            energy_spec=energy_spec,
            bin_time=bin_time,
            use_pi=use_pi,
            norm=norm,
            ref_band=ref_band,
            return_complex=False,
            segment_size=segment_size,
            events2=events2,
        )
