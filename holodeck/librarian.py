"""
"""

import abc
import argparse
from pathlib import Path
from datetime import datetime
import psutil
import resource
import os
import shutil
import sys

import h5py
import numpy as np
import scipy as sp
import scipy.optimize  # noqa
import matplotlib.pyplot as plt
import tqdm

from scipy.stats import qmc

import holodeck as holo
import holodeck.single_sources as ss
from holodeck import log, utils
from holodeck.constants import YR


__version__ = "0.3.0"

# Default argparse parameters
DEF_NUM_REALS = 100
DEF_NUM_FBINS = 40
DEF_PTA_DUR = 16.03     # [yrs]

FITS_NBINS_PLAW = [2, 3, 4, 5, 8, 9, 14]
FITS_NBINS_TURN = [4, 9, 14, 30]

FNAME_SIM_FILE_GWB_SS = "lib-sams_gwb-ss__p{pnum:06d}.npz"
# FNAME_SS_FILE = "lib_ss__p{pnum:06d}.npz"
PSPACE_FILE_SUFFIX = ".pspace.npz"

ALSO_PRODUCE_LEGACY_GWB_CALCULATION = True


class _Param_Space(abc.ABC):

    _SAVED_ATTRIBUTES = ["sam_shape", "param_names", "_uniform_samples", "param_samples"]

    def __init__(self, log, nsamples, sam_shape, seed, **kwargs):
        log.debug(f"seed = {seed}")
        np.random.seed(seed)
        # NOTE: this should be saved to output
        random_state = np.random.get_state()
        # log.debug(f"Random state is:\n{random_state}")

        param_names = list(kwargs.keys())
        ndims = len(param_names)
        if ndims == 0:
            err = f"No parameters passed to {self}!"
            log.exception(err)
            raise RuntimeError(err)

        dists = []
        for nam in param_names:
            val = kwargs[nam]

            # if not isinstance(val, _Param_Dist):
            # NOTE: this is a hacky check to see if `val` inherits from `_Param_Dist`
            #       it does this just by checking the string names, but it should generally work.
            #       The motivation is to make this work more easily for changing classes and modules.
            mro = val.__class__.__mro__
            mro = [mm.__name__ for mm in mro]
            if holo.librarian._Param_Dist.__name__ not in mro:
                err = f"{nam}: {val} is not a `_Param_Dist` object!"
                log.exception(err)
                raise ValueError(err)

            dists.append(val)

        # if strength = 2, then n must be equal to p**2, with p prime, and d <= p + 1
        lhs = qmc.LatinHypercube(d=ndims, centered=False, strength=1, seed=seed)
        # (S, D) - samples, dimensions
        uniform_samples = lhs.random(n=nsamples)
        param_samples = np.zeros_like(uniform_samples)

        for ii, dist in enumerate(dists):
            param_samples[:, ii] = dist(uniform_samples[:, ii])

        self._log = log
        self.param_names = param_names
        self.sam_shape = sam_shape
        self.param_samples = param_samples
        self._dists = dists
        self._uniform_samples = uniform_samples
        self._seed = seed
        self._random_state = random_state
        return

    def save(self, path_output):
        """Save the generated samples and parameter-space info from this instance to an output file.

        This data can then be loaded using the `_Param_Space.from_save` method.

        Arguments
        ---------
        path_output : str
            Path in which to save file.  This must be an existing directory.

        Returns
        -------
        fname : str
            Output path including filename in which this parameter-space was saved.

        """
        log = self._log
        my_name = self.__class__.__name__
        vers = __version__

        # make sure `path_output` is a directory, and that it exists
        path_output = Path(path_output)
        if not path_output.exists() or not path_output.is_dir():
            err = f"save path {path_output} does not exist, or is not a directory!"
            log.exception(err)
            raise ValueError(err)

        fname = f"{my_name}{PSPACE_FILE_SUFFIX}"
        fname = path_output.joinpath(fname)
        log.debug(f"{my_name=} {vers=} {fname=}")

        data = {}
        for key in self._SAVED_ATTRIBUTES:
            data[key] = getattr(self, key)

        np.savez(
            fname, class_name=my_name, librarian_version=vers,
            **data,
        )

        log.info(f"Saved to {fname} size {utils.get_file_size(fname)}")
        return fname

    @classmethod
    def from_save(cls, fname, log):
        """Create a new _Param_Space instance loaded from the given file.

        Arguments
        ---------
        fname : str
            Filename containing parameter-space save information, generated form `_Param_Space.save`.

        Returns
        -------
        space : `_Param_Space` instance

        """
        log.debug(f"loading parameter space from {fname}")
        data = np.load(fname)

        # get the name of the parameter-space class from the file, and try to find this class in the
        # `holodeck.param_spaces` module
        class_name = data['class_name'][()]
        log.debug(f"loaded: {class_name=}, vers={data['librarian_version']}")
        pspace_class = getattr(holo.param_spaces, class_name, None)
        # if it is not found, default to the current class/subclass
        if pspace_class is None:
            log.warning(f"pspace file {fname} has {class_name=}, not found in `holo.param_spaces`!")
            pspace_class = cls

        # construct instance with dummy/temporary values (which will be overwritten)
        space = pspace_class(log, 10, 10, None)
        if class_name != space.__class__.__name__:
            err = "loaded class name '{class_name}' does not match this class name '{space.__name__}'!"
            log.warning(err)
            # raise RuntimeError(err)

        # Store loaded parameters into the parameter-space instance
        for key in space._SAVED_ATTRIBUTES:
            setattr(space, key, data[key][()])

        return space

    def params(self, samp_num):
        return self.param_samples[samp_num]

    def param_dict(self, samp_num):
        rv = {nn: pp for nn, pp in zip(self.param_names, self.params(samp_num))}
        return rv

    def __call__(self, samp_num):
        return self.model_for_number(samp_num)

    @property
    def shape(self):
        return self.param_samples.shape

    @property
    def nsamples(self):
        return self.shape[0]

    @property
    def npars(self):
        return self.shape[1]

    def model_for_number(self, samp_num):
        params = self.param_dict(samp_num)
        self._log.debug(f"params {samp_num} :: {params}")
        return self.model_for_params(params, self.sam_shape)

    def model_for_normalized_params(self, vals, **kwargs):
        """Construct a model from this space by specifying fractional parameter values [0.0, 1.0].

        Arguments
        ---------
        vals : (P,) array_like  or  scalar
            Specification for each of `P` parameters varied in the parameter-space.  Each `vals` gives the
            location in uniform space between [0.0, 1.0] that will be converted to the parameter values
            based on the mapping the corresponding _Param_Dist instances (stored in `space._dists`).
            For example, if the 0th parameter uses a PD_Uniform_Log distribution, then a `vals` of 0.5
            for that parameter will correspond to half-way in log-space of the range of parameter values.
            If a scalar value is given, then it is used for each of the `P` parameters in the space.

        Returns
        -------
        sam : `holodeck.sam.Semi_Analytic_Model` instance
        hard : `holodeck.hardening._Hardening` instance

        """
        if np.ndim(vals) == 0:
            vals = self.npars * [vals]
        assert len(vals) == self.npars

        params = {}
        for ii, pname in enumerate(self.param_names):
            vv = vals[ii]    # desired fractional parameter value [0.0, 1.0]
            ss = self._dists[ii](vv)    # convert to actual parameter values
            params[pname] = ss           # store to dictionary

        return self.model_for_params(params, **kwargs)

    @classmethod
    @abc.abstractmethod
    def model_for_params(cls, params, **kwargs):
        raise


class _Param_Dist(abc.ABC):
    """Parameter Distribution classes for use in Latin HyperCube sampling.

    These classes are passed uniform random variables, and return the desired distributions of parameters.

    """

    def __init__(self, clip=None):
        if clip is not None:
            assert len(clip) == 2
        self._clip = clip
        return

    def __call__(self, xx):
        rv = self._dist_func(xx)
        if self._clip is not None:
            rv = np.clip(rv, *self._clip)
        return rv

    @property
    def extrema(self):
        return self(np.asarray([0.0, 1.0]))


class PD_Uniform(_Param_Dist):

    def __init__(self, lo, hi, **kwargs):
        super().__init__(**kwargs)
        self._lo = lo
        self._hi = hi
        # self._dist_func = lambda xx: self._lo + (self._hi - self._lo) * xx
        return

    def _dist_func(self, xx):
        yy = self._lo + (self._hi - self._lo) * xx
        return yy


class PD_Uniform_Log(_Param_Dist):

    def __init__(self, lo, hi, **kwargs):
        super().__init__(**kwargs)
        assert lo > 0.0 and hi > 0.0
        self._lo = np.log10(lo)
        self._hi = np.log10(hi)
        # self._dist_func = lambda xx: np.power(10.0, self._lo + (self._hi - self._lo) * xx)
        return

    def _dist_func(self, xx):
        yy = np.power(10.0, self._lo + (self._hi - self._lo) * xx)
        return yy


class PD_Normal(_Param_Dist):
    """

    NOTE: use `clip` parameter to avoid extreme values.

    """

    def __init__(self, mean, stdev, clip=None, **kwargs):
        """

        Arguments
        ---------

        """
        assert stdev > 0.0
        super().__init__(clip=clip, **kwargs)
        self._mean = mean
        self._stdev = stdev
        self._frozen_dist = sp.stats.norm(loc=mean, scale=stdev)
        return

    def _dist_func(self, xx):
        yy = self._frozen_dist.ppf(xx)
        return yy


class PD_Lin_Log(_Param_Dist):

    def __init__(self, lo, hi, crit, lofrac, **kwargs):
        """Distribute linearly below a cutoff, and then logarithmically above.

        Parameters
        ----------
        lo : float,
            lowest output value (in linear space)
        hi : float,
            highest output value (in linear space)
        crit : float,
            Location of transition from log to lin scaling.
        lofrac : float,
            Fraction of mass below the cutoff.

        """
        super().__init__(**kwargs)
        self._lo = lo
        self._hi = hi
        self._crit = crit
        self._lofrac = lofrac
        return

    def _dist_func(self, xx):
        lo = self._lo
        crit = self._crit
        lofrac = self._lofrac
        l10_crit = np.log10(crit)
        l10_hi = np.log10(self._hi)
        xx = np.atleast_1d(xx)
        yy = np.empty_like(xx)

        # select points below the cutoff
        loidx = (xx <= lofrac)
        # transform to linear-scaling between [lo, crit]
        yy[loidx] = lo + xx[loidx] * (crit - lo) / lofrac

        # select points above the cutoff
        hiidx = ~loidx
        # transform to log-scaling between [crit, hi]
        temp = l10_crit + (l10_hi - l10_crit) * (xx[hiidx] - lofrac) / (1 - lofrac)
        yy[hiidx] = np.power(10.0, temp)
        return yy


class PD_Log_Lin(_Param_Dist):

    def __init__(self, lo, hi, crit, lofrac, **kwargs):
        """Distribute logarithmically below a cutoff, and then linearly above.

        Parameters
        ----------
        lo : float,
            lowest output value (in linear space)
        hi : float,
            highest output value (in linear space)
        crit : float,
            Location of transition from log to lin scaling.
        lofrac : float,
            Fraction of mass below the cutoff.

        """
        super().__init__(**kwargs)
        self._lo = lo
        self._hi = hi
        self._crit = crit
        self._lofrac = lofrac
        return

    def _dist_func(self, xx):
        hi = self._hi
        crit = self._crit
        lofrac = self._lofrac
        l10_lo = np.log10(self._lo)
        l10_crit = np.log10(crit)

        xx = np.atleast_1d(xx)
        yy = np.empty_like(xx)

        # select points below the cutoff
        loidx = (xx <= lofrac)
        # transform to log-scaling between [lo, crit]
        temp = l10_lo + (l10_crit - l10_lo) * xx[loidx] / lofrac
        yy[loidx] = np.power(10.0, temp)

        # select points above the cutoff
        hiidx = ~loidx
        # transform to lin-scaling between [crit, hi]
        yy[hiidx] = crit + (hi - crit) * (xx[hiidx] - lofrac) / (1.0 - lofrac)
        return yy


class PD_Piecewise_Uniform_Mass(_Param_Dist):

    def __init__(self, edges, weights, **kwargs):
        super().__init__(**kwargs)
        edges = np.asarray(edges)
        self._edges = edges
        weights = np.asarray(weights)
        self._weights = weights / weights.sum()
        assert edges.size == weights.size + 1
        assert np.ndim(edges) == 1
        assert np.ndim(weights) == 1
        assert np.all(np.diff(edges) > 0.0)
        assert np.all(weights > 0.0)
        return

    def _dist_func(self, xx):
        yy = np.zeros_like(xx)
        xlo = 0.0
        for ii, ww in enumerate(self._weights):
            ylo = self._edges[ii]
            yhi = self._edges[ii+1]

            xhi = xlo + ww
            sel = (xlo < xx) & (xx <= xhi)
            yy[sel] = ylo + (xx[sel] - xlo) * (yhi - ylo) / (xhi - xlo)

            xlo = xhi

        return yy


class PD_Piecewise_Uniform_Density(PD_Piecewise_Uniform_Mass):

    def __init__(self, edges, densities, **kwargs):
        dx = np.diff(edges)
        weights = dx * np.asarray(densities)
        super().__init__(edges, weights)
        return


def load_pspace_from_dir(path, space_class=None):
    """Load a _Param_Space instance from the saved file in the given directory.

    Arguments
    ---------
    path : str or pathlib.Path
        Path to directory containing save file.
        A single file matching "*.pspace.npz" is required in that directory.
        NOTE: the specific glob pattern is specified by `holodeck.librarian.PSPACE_FILE_SUFFIX` e.g. '.pspace.npz'
    space_class : _Param_Space subclass
        Class with which to call the `from_save()` method to load a new _Param_Space instance.

    Returns
    -------
    space : `_Param_Space` subclass instance
        An instance of the `space_class` class.
    space_fname : pathlib.Path
        File that `space` was loaded from.

    """
    path = Path(path)
    if not path.exists() or not path.is_dir():
        raise RuntimeError(f"path {path} is not an existing directory!")

    pattern = "*" + holo.librarian.PSPACE_FILE_SUFFIX
    space_fname = list(path.glob(pattern))
    if len(space_fname) != 1:
        raise FileNotFoundError(f"found {len(space_fname)} matches to {pattern} in output {path}!")

    space_fname = space_fname[0]
    # Based on the `space_fname`, try to find a matching PS (parameter-space) in `holodeck.param_spaces`
    if space_class is None:
        # get the filename without path, this should contain the name of the PS class
        space_name = space_fname.name
        # get a list of all parameter-space classes (assuming they all start with 'PS')
        space_list = [ss for ss in dir(holo.param_spaces) if ss.startswith('PS')]
        # iterate over space classes to try to find a match
        for space in space_list:
            # exist for-loop if the names match
            # NOTE: previously the save files converted class names to lower-case; that should no
            #       longer be the case, but use `lower()` for backwards compatibility at the moment
            #       LZK 2023-05-10
            if space.lower() in space_name.lower():
                break
        else:
            raise ValueError(f"Unable to find a PS class matching {space_name}!")

        space_class = getattr(holo.param_spaces, space)

    space = space_class.from_save(space_fname, log)
    return space, space_fname


def sam_lib_combine(path_output, log, path_sims=None, path_pspace=None):
    """

    Arguments
    ---------
    path_output : str or Path,
        Path to output directory where combined library will be saved.
    log : `logging.Logger`
        Logging instance.
    path_sims : str or None,
        Path to output directory containing simulation files.
        If `None` this is set to be the same as `path_output`.
    path_pspace : str or None,
        Path to file containing _Param_Space subclass instance.
        If `None` then `path_output` is searched for a `_Param_Space` save file.

    Returns
    -------
    out_filename : Path,
        Path to library output filename (typically ending with 'sam_lib.hdf5').

    """

    # ---- setup paths

    path_output = Path(path_output)
    log.info(f"Path output = {path_output}")
    # if dedicated simulation path is not given, assume same as general output path
    if path_sims is None:
        path_sims = path_output
    path_sims = Path(path_sims)
    log.info(f"Path sims = {path_sims}")

    # ---- load parameter space from save file

    if path_pspace is None:
        # look for parameter-space save files
        regex = "*" + PSPACE_FILE_SUFFIX   # "*.pspace.npz"
        files = sorted(path_output.glob(regex))
        num_files = len(files)
        msg = f"found {num_files} pspace.npz files in {path_output}"
        log.info(msg)
        if num_files != 1:
            log.exception(f"")
            log.exception(msg)
            log.exception(f"{files=}")
            log.exception(f"{regex=}")
            raise RuntimeError(f"{msg}")
        path_pspace = files[0]

    pspace = _Param_Space.from_save(path_pspace, log)
    log.info(f"loaded param space: {pspace}")
    param_names = pspace.param_names
    param_samples = pspace.param_samples
    nsamp, ndim = param_samples.shape
    log.debug(f"{nsamp=}, {ndim=}, {param_names=}")

    # ---- make sure all files exist; get shape information from files

    log.info(f"checking that all {nsamp} files exist")
    fobs, nreals, nloudest, fit_data, has_gwb = _check_files_and_load_shapes(path_sims, nsamp)
    nfreqs = fobs.size
    fit_keys = fit_data.keys() if fit_data is not None else None
    log.debug(f"{nfreqs=}, {nreals=}, {nloudest=}, {fit_keys=}")

    # ---- Store results from all files

    gwb = np.zeros((nsamp, nfreqs, nreals)) if has_gwb else None
    hc_ss = np.zeros((nsamp, nfreqs, nreals, nloudest))
    hc_bg = np.zeros((nsamp, nfreqs, nreals))
    sspar = np.zeros((nsamp, 3, nfreqs, nreals, nloudest))
    bgpar = np.zeros((nsamp, 3, nfreqs, nreals))
    gwb, hc_ss, hc_bg, fit_data, sspar, bgpar, bad_files = _load_library_from_all_files(
        path_sims, gwb, hc_ss, hc_bg, fit_data, sspar, bgpar, log
    )
    log.info(f"Loaded data from all library files | {utils.stats(gwb)=}")
    param_samples[bad_files] = np.nan

    # ---- Save to concatenated output file ----

    out_filename = path_output.joinpath('sam_lib.hdf5')
    log.info(f"Writing collected data to file {out_filename}")
    with h5py.File(out_filename, 'w') as h5:
        h5.create_dataset('fobs', data=fobs)
        if gwb is not None:
            h5.create_dataset('gwb', data=gwb)
        h5.create_dataset('hc_ss', data=hc_ss)
        h5.create_dataset('hc_bg', data=hc_bg)
        h5.create_dataset('sspar', data=sspar)
        h5.create_dataset('bgpar', data=bgpar)
        h5.create_dataset('sample_params', data=param_samples)
        for kk, vv in fit_data.items():
            h5.create_dataset(kk, data=vv)
        h5.attrs['param_names'] = np.array(param_names).astype('S')

    log.warning(f"Saved to {out_filename}, size: {holo.utils.get_file_size(out_filename)}")

    return out_filename


def _check_files_and_load_shapes(path_sims, nsamp):
    """Check that all `nsamp` files exist in the given path, and load info about array shapes.

    Arguments
    ---------
    path_sims : str
        Path in which individual simulation files can be found.
    nsamp : int
        Number of simulations/files that should be found.
        This should typically be loaded from the parameter-space object used to generate the library.

    Returns
    -------
    fobs : (F,) ndarray
        Observer-frame frequency bin centers at which GW signals are calculated.
    nreals : int
        Number of realizations in the output files.
    fit_data : dict
        Dictionary where each key is a fit-parameter in all of the output files.  The values are
        'ndarray's of the appropriate shapes to store fit-parameters from all files.
        The 0th dimension is always for the number-of-files.

    """

    fobs = None
    nreals = None
    nloudest = None
    fit_data = None
    has_gwb = False
    for ii in tqdm.trange(nsamp):
        temp = _get_sim_fname_gwb_ss(path_sims, ii)
        if not temp.exists():
            err = f"Missing at least file number {ii} out of {nsamp} files!  {temp}"
            log.exception(err)
            raise ValueError(err)

        # if we've already loaded all of the necessary info, then move on to the next file
        if (fobs is not None) and (nreals is not None) and (fit_data is not None) and (nloudest is not None):
            continue

        temp = np.load(temp)
        data_keys = temp.keys()

        if fobs is None:
            fobs = temp['fobs'][()]

        if (not has_gwb) and ('gwb' in data_keys):
            has_gwb = True

        # find a file that has GWB data in it (not all of them do, if the file was a 'failure' file)
        if (nreals is None):
            nreals_1 = None
            nreals_2 = None
            if ('gwb' in data_keys):
                nreals_1 = temp['gwb'].shape[-1]
                nreals = nreals_1
            if ('hc_bg' in data_keys):
                nreals_2 == temp['hc_bg'].shape[-1]
                nreals = nreals_2
            if (nreals_1 is not None) and (nreals_2 is not None):
                assert nreals_1 == nreals_2

        if (nloudest is None) and ('hc_ss' in data_keys):
            nloudest = temp['hc_ss'].shape[-1]

        # find a file that has fits data in it (it's possible for the fits portion to fail by itself)
        # initialize arrays to store output data for all files
        if (fit_data is None) and np.any([kk.startswith('fit_') for kk in data_keys]):
            fit_data = {}
            for kk in data_keys:
                if not kk.startswith('fit_'):
                    continue

                vv = temp[kk]
                # arrays need to store values for 'nsamp' files
                shape = (nsamp,) + vv.shape
                fit_data[kk] = np.zeros(shape)

    if fit_data is not None:
        for kk, vv in fit_data.items():
            log.debug(f"\t{kk:>20s}: {vv.shape}")
    else:
        log.warning("Unable to load `fit_data` from any files!")
        fit_data = {}

    return fobs, nreals, nloudest, fit_data, has_gwb


def _load_library_from_all_files(path_sims, gwb, hc_ss, hc_bg, fit_data, sspar, bgpar, log):
    """Load data from all individual simulation files.

    Arguments
    ---------
    path_sims : str
        Path to find individual simulation files.
    gwb : (S, F, R) ndarray
        Array in which to store GWB data from all of 'S' files.
        S: num-samples/simulations,  F: num-frequencies,  R: num-realizations.
    fit_data : dict
        Dictionary of ndarrays in which to store fit-parameters.
    log : `logging.Logger`
        Logging instance.

    """

    nsamp = hc_bg.shape[0]
    log.info(f"Collecting data from {nsamp} files")
    bad_files = np.zeros(nsamp, dtype=bool)     #: track which files contain UN-useable data
    num_fits_failed = 0
    msg = None
    for pnum in tqdm.trange(nsamp):
        fname = _get_sim_fname_gwb_ss(path_sims, pnum)
        temp = np.load(fname, allow_pickle=True)
        # When a processor fails for a given parameter, the output file is still created with the 'fail' key added
        if ('fail' in temp):
            msg = f"file {pnum=:06d} is a failure file, setting values to NaN ({fname})"
            log.warning(msg)
            # set all parameters to NaN for failure files.  Note that this is distinct from gwb=0.0 which can be real.
            if gwb is not None:
                gwb[pnum, :, :] = np.nan
            hc_ss[pnum, :, :, :] = np.nan
            hc_bg[pnum, :, :] = np.nan
            for fk in fit_data.keys():
                fit_data[fk][pnum, ...] = np.nan

            bad_files[pnum] = True
            continue

        # store the GWB from this file
        if gwb is not None:
            gwb[pnum, :, :] = temp['gwb'][...]
        hc_ss[pnum, :, :, :] = temp['hc_ss'][...]
        hc_bg[pnum, :, :] = temp['hc_bg'][...]
        sspar[pnum, :, :, :, :] = temp['sspar'][...]
        bgpar[pnum, :, :, :] = temp['bgpar'][...]

        # store all of the fit data
        fits_bad = False
        for fk in fit_data.keys():
            try:
                fit_data[fk][pnum, ...] = temp[fk][...]
            except Exception as err:
                # only count the first time it fails
                if not fits_bad:
                    num_fits_failed += 1
                fits_bad = True
                fit_data[fk][pnum, ...] = np.nan
                msg = str(err)

        if fits_bad:
            log.warning(f"Missing fit keys in file {pnum} = {fname.name}")

    if num_fits_failed > 0:
        log.warning(f"Missing fit keys in {num_fits_failed}/{nsamp} = {num_fits_failed/nsamp:.2e} files!")
        log.warning(msg)

    log.info(f"{utils.frac_str(bad_files)} files are failures")

    return gwb, hc_ss, hc_bg, fit_data, sspar, bgpar, bad_files


def _fit_spectra(freqs, psd, nbins, nfit_pars, fit_func, min_nfreq_valid):
    nfreq, nreals = np.shape(psd)
    assert len(freqs) == nfreq

    def fit_if_all_finite(xx, yy):
        if np.any(~np.isfinite(yy)):
            pars = [np.nan] * nfit_pars
        else:
            sel = (yy > 0.0)
            if np.count_nonzero(sel) < min_nfreq_valid:
                pars = [np.nan] * nfit_pars
            else:
                pars = fit_func(xx[sel], yy[sel])
        return pars

    nfreq_bins = len(nbins)
    fit_pars = np.zeros((nfreq_bins, nreals, nfit_pars))
    fit_med_pars = np.zeros((nfreq_bins, nfit_pars))
    for ii, num in enumerate(nbins):
        if num > nfreq:
            raise ValueError(f"Cannot fit for {num=} bins, data has {nfreq=} frequencies!")

        num = None if (num == 0) else num
        cut = slice(None, num)
        xx = freqs[cut]

        # fit the median spectra
        yy = np.median(psd, axis=-1)[cut]
        fit_med_pars[ii] = fit_if_all_finite(xx, yy)

        # fit each realization of the spectra
        for rr in range(nreals):
            yy = psd[cut, rr]
            fit_pars[ii, rr, :] = fit_if_all_finite(xx, yy)

    return nbins, fit_pars, fit_med_pars


def fit_spectra_plaw(freqs, psd, nbins):
    fit_func = lambda xx, yy: utils.fit_powerlaw_psd(xx, yy, 1/YR)
    nfit_pars = 2
    # min_nfreq_valid = 2
    min_nfreq_valid = nfit_pars
    return _fit_spectra(freqs, psd, nbins, nfit_pars, fit_func, min_nfreq_valid)


def fit_spectra_turn(freqs, psd, nbins):
    fit_func = lambda xx, yy: utils.fit_turnover_psd(xx, yy, 1/YR)
    nfit_pars = 4
    # min_nfreq_valid = 3
    min_nfreq_valid = nfit_pars
    return _fit_spectra(freqs, psd, nbins, nfit_pars, fit_func, min_nfreq_valid)


def fit_spectra_plaw_hc(freqs, gwb, nbins):
    fit_func = lambda xx, yy: utils.fit_powerlaw(xx, yy)
    nfit_pars = 2
    # min_nfreq_valid = 2
    min_nfreq_valid = nfit_pars
    return _fit_spectra(freqs, gwb, nbins, nfit_pars, fit_func, min_nfreq_valid)


def make_gwb_plot(fobs, gwb, fit_data):
    # fig = holo.plot.plot_gwb(fobs, gwb)
    psd = utils.char_strain_to_psd(fobs[:, np.newaxis], gwb)
    fig = holo.plot.plot_gwb(fobs, psd)
    ax = fig.axes[0]

    xx = fobs * YR
    yy = 1e-15 * np.power(xx, -2.0/3.0)
    ax.plot(xx, yy, 'k--', alpha=0.5, lw=1.0, label=r"$10^{-15} \cdot f_\mathrm{yr}^{-2/3}$")

    if len(fit_data) > 0:
        fit_nbins = fit_data['fit_plaw_nbins']
        med_pars = fit_data['fit_plaw_med']

        plot_nbins = [4, 14]

        for nbins in plot_nbins:
            idx = fit_nbins.index(nbins)
            pars = med_pars[idx]

            pars[0] = 10.0 ** pars[0]
            yy = holo.utils._func_powerlaw_psd(fobs, 1/YR, *pars)
            label = fit_nbins[idx]
            label = 'all' if label in [0, None] else f"{label:02d}"
            ax.plot(xx, yy, alpha=0.75, lw=1.0, label="plaw: " + str(label) + " bins", ls='--')

        fit_nbins = fit_data['fit_turn_nbins']
        med_pars = fit_data['fit_turn_med']

        plot_nbins = [14, 30]

        for nbins in plot_nbins:
            idx = fit_nbins.index(nbins)
            pars = med_pars[idx]

            pars[0] = 10.0 ** pars[0]
            zz = holo.utils._func_turnover_psd(fobs, 1/YR, *pars)
            label = fit_nbins[idx]
            label = 'all' if label in [0, None] else f"{label:02d}"
            ax.plot(xx, zz, alpha=0.75, lw=1.0, label="turn: " + str(label) + " bins")

    ax.legend(fontsize=6, loc='upper right')

    return fig


def make_ss_plot(fobs, hc_ss, hc_bg, fit_data):
    # fig = holo.plot.plot_gwb(fobs, gwb)
    psd_bg = utils.char_strain_to_psd(fobs[:, np.newaxis], hc_bg)
    psd_ss = utils.char_strain_to_psd(fobs[:, np.newaxis, np.newaxis], hc_ss)
    fig = holo.plot.plot_bg_ss(fobs, bg=psd_bg, ss=psd_ss, ylabel='GW Power Spectral Density')
    ax = fig.axes[0]

    xx = fobs * YR
    yy = 1e-15 * np.power(xx, -2.0/3.0)
    ax.plot(xx, yy, 'k--', alpha=0.5, lw=1.0, label=r"$10^{-15} \cdot f_\mathrm{yr}^{-2/3}$")

    if len(fit_data) > 0:
        fit_nbins = fit_data['fit_plaw_nbins']
        med_pars = fit_data['fit_plaw_med']

        plot_nbins = [4, 14]

        for nbins in plot_nbins:
            idx = fit_nbins.index(nbins)
            pars = med_pars[idx]

            pars[0] = 10.0 ** pars[0]
            yy = holo.utils._func_powerlaw_psd(fobs, 1/YR, *pars)
            label = fit_nbins[idx]
            label = 'all' if label in [0, None] else f"{label:02d}"
            ax.plot(xx, yy, alpha=0.75, lw=1.0, label="plaw: " + str(label) + " bins", ls='--')

        fit_nbins = fit_data['fit_turn_nbins']
        med_pars = fit_data['fit_turn_med']

        plot_nbins = [14, 30]

        for nbins in plot_nbins:
            idx = fit_nbins.index(nbins)
            pars = med_pars[idx]

            pars[0] = 10.0 ** pars[0]
            zz = holo.utils._func_turnover_psd(fobs, 1/YR, *pars)
            label = fit_nbins[idx]
            label = 'all' if label in [0, None] else f"{label:02d}"
            ax.plot(xx, zz, alpha=0.75, lw=1.0, label="turn: " + str(label) + " bins")

    ax.legend(fontsize=6, loc='upper right')

    return fig


def make_pars_plot(fobs, hc_ss, hc_bg, sspar, bgpar):
    # fig = holo.plot.plot_gwb(fobs, gwb)
    fig = holo.plot.plot_pars(fobs, hc_ss, hc_bg, sspar, bgpar)
    ax = fig.axes[3]

    xx = fobs * YR
    yy = 1e-15 * np.power(xx, -2.0/3.0)
    ax.plot(xx, yy, 'k--', alpha=0.5, lw=1.0, label=r"$10^{-15} \cdot f_\mathrm{yr}^{-2/3}$")

    ax.legend(fontsize=6, loc='upper right')

    return fig


def run_sam_at_pspace_num(args, space, pnum):
    """Run single source and background strain calculations for the SAM simulation
    for sample-parameter `pnum` in the `space` parameter-space.
    Arguments
    ---------
    args : `argparse.ArgumentParser` instance
        Arguments from the `gen_lib_sams.py` script.
        NOTE: this should be improved.
    space : _Param_Space instance
        Parameter space from which to load `sam` and `hard` instances.
    pnum : int
        Which parameter-sample from `space` should be run.
    Returns
    -------
    rv : bool
        True if this simulation was successfully run.
    """

    log = args.log

    # ---- get output filename for this simulation, check if already exists

    sim_fname = _get_sim_fname_gwb_ss(args.output_sims, pnum)

    beg = datetime.now()
    log.info(f"{pnum=} :: {sim_fname=} beginning at {beg}")

    if sim_fname.exists():
        log.info(f"File {sim_fname} already exists.  {args.recreate=}")
        # skip existing files unless we specifically want to recreate them
        if not args.recreate:
            return True

    # ---- Setup PTA frequencies

    pta_dur = args.pta_dur * YR
    nfreqs = args.nfreqs
    hifr = nfreqs/pta_dur
    pta_cad = 1.0 / (2 * hifr)
    fobs_cents = holo.utils.nyquist_freqs(pta_dur, pta_cad)
    fobs_edges = holo.utils.nyquist_freqs_edges(pta_dur, pta_cad)
    log.info(f"Created {fobs_cents.size} frequency bins")
    log.info(f"\t[{fobs_cents[0]*YR}, {fobs_cents[-1]*YR}] [1/yr]")
    log.info(f"\t[{fobs_cents[0]*1e9}, {fobs_cents[-1]*1e9}] [nHz]")
    _log_mem_usage(log)
    assert nfreqs == fobs_cents.size

    # ---- Calculate hc_ss, hc_bg, sspar, and bgpar from SAM

    try:
        log.debug("Selecting `sam` and `hard` instances")
        sam, hard = space(pnum)
        _log_mem_usage(log)

        log.debug(f"Calculating 'edges' and 'number' for this SAM.")
        fobs_orb_edges = fobs_edges / 2.0
        fobs_orb_cents = fobs_cents / 2.0
        # edges
        # should the zero stalled option be part of the parameter space?
        edges, dnum = sam.dynamic_binary_number(hard, fobs_orb=fobs_orb_cents)
        edges[-1] = fobs_orb_edges
        # integrate for number
        number = utils._integrate_grid_differential_number(edges, dnum, freq=False)
        number = number * np.diff(np.log(fobs_edges))
        _log_mem_usage(log)

        try:
            use_redz = sam._redz_final
            log.info("using `redz_final`")
        except AttributeError:
            use_redz = sam._redz_prime[:, :, :, np.newaxis]
            log.warning("using `redz_prime`")

        log.debug(f"Calculating `ss_gws` for shape ({fobs_cents.size}, {args.nreals})")
        hc_ss, hc_bg, sspar, bgpar = ss.ss_gws_redz(
            edges, use_redz, number, realize=args.nreals,
            loudest=args.nloudest, params=True
        )
        log.debug(f"{holo.utils.stats(hc_ss)=}")
        log.debug(f"{holo.utils.stats(hc_bg)=}")

        _log_mem_usage(log)

        log.debug(f"Calculating `gwb` for shape ({fobs_cents.size}, {args.nreals})")
        if ALSO_PRODUCE_LEGACY_GWB_CALCULATION:
            gwb = holo.gravwaves._gws_from_number_grid_integrated_redz(edges, use_redz, number, args.nreals)
            log.debug(f"{holo.utils.stats(gwb)=}")

        _log_mem_usage(log)

        log.debug(f"Saving {pnum} to file")
        data = dict(fobs=fobs_cents, fobs_edges=fobs_edges,
                    hc_ss=hc_ss, hc_bg=hc_bg, sspar=sspar, bgpar=bgpar)
        if ALSO_PRODUCE_LEGACY_GWB_CALCULATION:
            data['gwb'] = gwb

        rv = True
    except Exception as err:
        log.exception(f"`run_ss` FAILED on {pnum=}\n")
        log.exception(err)
        rv = False
        data = dict(fail=str(err))

    # ---- Save data to file

    np.savez(sim_fname, **data)
    log.info(f"Saved to {sim_fname}, size {holo.utils.get_file_size(sim_fname)} after {(datetime.now()-beg)}")

    # ---- Plot hc and pars

    if rv and args.plot:
        log.info("generating characteristic strain/psd plots")
        try:
            log.info("generating strain plots")
            plot_fname = args.output_plots.joinpath(sim_fname.name)
            hc_fname = str(plot_fname.with_suffix(''))+"_strain.png"
            fig = holo.plot.plot_bg_ss(fobs_cents, bg=hc_bg, ss=hc_ss)
            fig.savefig(hc_fname, dpi=100)
            # log.info("generating PSD plots")
            # psd_fname = str(plot_fname.with_suffix('')) + "_psd.png"
            # fig = make_ss_plot(fobs_cents, hc_ss, hc_bg, fit_data)
            # fig.savefig(psd_fname, dpi=100)
            # log.info(f"Saved to {psd_fname}, size {holo.utils.get_file_size(psd_fname)}")
            log.info("generating pars plots")
            pars_fname = str(plot_fname.with_suffix('')) + "_pars.png"
            fig = make_pars_plot(fobs_cents, hc_ss, hc_bg, sspar, bgpar)
            fig.savefig(pars_fname, dpi=100)
            log.info(f"Saved to {pars_fname}, size {holo.utils.get_file_size(pars_fname)}")
            plt.close('all')
        except Exception as err:
            log.exception("Failed to make strain plot!")
            log.exception(err)

    return rv


def _get_sim_fname_gwb_ss(path, pnum):
    temp = FNAME_SIM_FILE_GWB_SS.format(pnum=pnum)
    temp = path.joinpath(temp)
    return temp


def _log_mem_usage(log):
    # results.ru_maxrss is KB on Linux, B on macos
    mem_max = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform.lower().startswith('darwin'):
        mem_max = (mem_max / 1024 ** 3)
    else:
        mem_max = (mem_max / 1024 ** 2)

    process = psutil.Process(os.getpid())
    mem_rss = process.memory_info().rss / 1024**3
    mem_vms = process.memory_info().vms / 1024**3

    msg = f"Current memory usage: max={mem_max:.2f} GB, RSS={mem_rss:.2f} GB, VMS={mem_vms:.2f} GB"
    if log is None:
        print(msg, flush=True)
    else:
        log.info(msg)

    return


def setup_basics(comm, copy_files=None):
    if comm.rank == 0:
        args = _setup_argparse(comm)
    else:
        args = None

    # share `args` to all processes
    args = comm.bcast(args, root=0)

    # setup log instance, separate for all processes
    log = _setup_log(comm, args)
    args.log = log

    if comm.rank == 0:
        # copy certain files to output directory
        if (not args.resume) and (copy_files is not None):
            for fname in copy_files:
                src_file = Path(fname)
                dst_file = args.output.joinpath("runtime_" + src_file.name)
                shutil.copyfile(src_file, dst_file)
                log.info(f"Copied {fname} to {dst_file}")

        # get parameter-space class
        try:
            # `param_space` attribute must match the name of one of the classes in `holo.param_spaces`
            space_class = getattr(holo.param_spaces, args.param_space)
        except Exception as err:
            log.exception(f"Failed to load '{args.param_space}' from holo.param_spaces!")
            log.exception(err)
            raise err

        # instantiate the parameter space class
        if args.resume:
            # Load pspace object from previous save
            space, space_fname = holo.librarian.load_pspace_from_dir(args.output, space_class)
            log.warning(f"resume={args.resume} :: Loaded param-space save from {space_fname}")
        else:
            space = space_class(log, args.nsamples, args.sam_shape, args.seed)
    else:
        space = None

    # share parameter space across processes
    space = comm.bcast(space, root=0)

    log.info(
        f"param_space={args.param_space}, samples={args.nsamples}, sam_shape={args.sam_shape}, nreals={args.nreals}\n"
        f"nfreqs={args.nfreqs}, pta_dur={args.pta_dur} [yr]\n"
    )

    return args, space, log


def _setup_argparse(comm, *args, **kwargs):
    assert comm.rank == 0

    parser = argparse.ArgumentParser()
    parser.add_argument('param_space', type=str,
                        help="Parameter space class name, found in 'holodeck.param_spaces'.")

    parser.add_argument('output', metavar='output', type=str,
                        help='output path [created if doesnt exist]')

    parser.add_argument('-n', '--nsamples', action='store', dest='nsamples', type=int, default=1000,
                        help='number of parameter space samples')
    parser.add_argument('-r', '--nreals', action='store', dest='nreals', type=int,
                        help='number of realiz  ations', default=DEF_NUM_REALS)
    parser.add_argument('-d', '--dur', action='store', dest='pta_dur', type=float,
                        help='PTA observing duration [yrs]', default=DEF_PTA_DUR)
    parser.add_argument('-f', '--nfreqs', action='store', dest='nfreqs', type=int,
                        help='Number of frequency bins', default=DEF_NUM_FBINS)
    parser.add_argument('-s', '--shape', action='store', dest='sam_shape', type=int,
                        help='Shape of SAM grid', default=None)
    parser.add_argument('-l', '--loudest', action='store', dest='nloudest', type=int,
                        help='Number of loudest single sources', default=5)

    parser.add_argument('--resume', action='store_true', default=False,
                        help='resume production of a library by loading previous parameter-space from output directory')
    parser.add_argument('--recreate', action='store_true', default=False,
                        help='recreating existing simulation files')
    parser.add_argument('--plot', action='store_true', default=False,
                        help='produce plots for each simulation configuration')
    parser.add_argument('--seed', action='store', type=int, default=None,
                        help='Random seed to use')
    # parser.add_argument('-t', '--test', action='store_true', default=False, dest='test',
    #                     help='Do not actually run, just output what parameters would have been done.')
    parser.add_argument('-v', '--verbose', action='store_true', default=False, dest='verbose',
                        help='verbose output [INFO]')

    args = parser.parse_args(*args, **kwargs)

    output = Path(args.output).resolve()
    if not output.is_absolute:
        output = Path('.').resolve() / output
        output = output.resolve()

    if args.resume:
        if not output.exists() or not output.is_dir():
            raise FileNotFoundError(f"`--resume` is active but output path does not exist! '{output}'")
    elif output.exists():
        raise RuntimeError(f"Output {output} already exists!  Overwritting not currently supported!")

    # ---- Create output directories as needed

    output.mkdir(parents=True, exist_ok=True)
    utils.my_print(f"output path: {output}")
    args.output = output

    output_sims = output.joinpath("sims")
    output_sims.mkdir(parents=True, exist_ok=True)
    args.output_sims = output_sims

    output_logs = output.joinpath("logs")
    output_logs.mkdir(parents=True, exist_ok=True)
    args.output_logs = output_logs

    if args.plot:
        output_plots = output.joinpath("figs")
        output_plots.mkdir(parents=True, exist_ok=True)
        args.output_plots = output_plots

    return args


def _setup_log(comm, args):
    beg = datetime.now()
    log_name = f"holodeck__gen_lib_sams_{beg.strftime('%Y%m%d-%H%M%S')}"
    if comm.rank > 0:
        log_name = f"_{log_name}_r{comm.rank}"

    output = args.output_logs
    fname = f"{output.joinpath(log_name)}.log"
    log_lvl = holo.logger.INFO if args.verbose else holo.logger.WARNING
    tostr = sys.stdout if comm.rank == 0 else False
    log = holo.logger.get_logger(name=log_name, level_stream=log_lvl, tofile=fname, tostr=tostr)
    log.info(f"Output path: {output}")
    log.info(f"        log: {fname}")
    log.info(args)
    return log


def main():

    from argparse import ArgumentParser

    parser = ArgumentParser()
    subparsers = parser.add_subparsers(dest="subcommand")

    combine = subparsers.add_parser('combine', help='combine output files')
    combine.add_argument(
        'path', default=None,
        help='library directory to run combination on; must contain the `sims` subdirectory'
    )
    combine.add_argument('--debug', '-d', action='store_true', default=False)

    args = parser.parse_args()
    log.debug(f"{args=}")

    if args.subcommand == 'combine':
        sam_lib_combine(args.path, log, path_sims=Path(args.path).joinpath('sims'))
    else:
        parser.print_help()
        sys.exit()

    return


if __name__ == "__main__":
    main()
