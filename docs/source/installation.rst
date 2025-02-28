Installation
============

Shenfun has a few dependencies

    * `mpi4py`_
    * `FFTW`_
    * `mpi4py-fft`_
    * `cython`_
    * `numpy`_
    * `sympy`_
    * `scipy`_
    * `h5py`_
    * `pyyaml`_

that are mostly straight-forward to install, or already installed in
most Python environments. The first two are usually most troublesome.
Basically, for `mpi4py`_ you need to have a working MPI installation,
whereas `FFTW`_ is available on most high performance computer systems.
If you are using `conda`_, then all you need to install a fully functional
shenfun, with all the above dependencies, is

::

    conda install -c conda-forge shenfun

You probably want to install into a fresh environment, though, which
can be achieved with

::

    conda create --name shenfun -c conda-forge shenfun
    conda activate shenfun

Note that this gives you shenfun with default settings. This means that
you will probably get the openmpi backend. To make sure that shenfun is
is installed with mpich instead do

::

    conda create --name shenfun -c conda-forge shenfun mpich

If you do not use `conda`_, then you need to make sure that MPI
and FFTW are installed by some other means. You can then install
any version of shenfun hosted on `pypi`_ using `pip`_

::

    pip install shenfun

whereas the following will install the latest version from github

::

    pip install git+https://github.com/spectralDNS/shenfun.git@master

Note that a common approach is to install ``shenfun`` from ``conda-forge`` to
get all the dependencies, and then build a local version by (after cloning or
forking to a local folder) running from the top directory

::

    pip install .

or

::

    python setup.py build_ext -i

This is required to build all the Cython dependencies locally.

Configuration
-------------

Shenfun comes with a few configuration options that can be found in the
`config.py <https://github.com/spectralDNS/shenfun/tree/master/shenfun/config.py>`_
file. If you want to make your own modifications to this configuration, there
are two options. Either create a file `shenfun.yaml` in the
local directory where you run your shenfun solver, or put a `shenfun.yaml`
file in the home directory `~/.shenfun/shenfun.yaml`. If a configuration
file in found in either of these to locations, the settings there will
overload the original. The local configuration file will be the last
to be read. To generate a baseline configuration file you can run the
`dumpconfig` function

::

    from shenfun.config import dumpconfig
    dumpconfig()

This will place a configuration file by default here: `~/.shenfun/shenfun.yaml`,
that you can then choose to modify. If you want a local file instead, then run
`dumpconfig(path='.')`. The yaml file looks and works like a dictionary.
At the time of writing the configuration looks like

::

    bases:
      jacobi:
        mode: numpy
    basisvectors: normal
    fftw:
      dct:
        planner_effort: FFTW_MEASURE
        threads: 1
      dlt:
        planner_effort: FFTW_MEASURE
        threads: 1
      dst:
        planner_effort: FFTW_MEASURE
        threads: 1
      fft:
        planner_effort: FFTW_MEASURE
        threads: 1
      ifft:
        planner_effort: FFTW_MEASURE
        threads: 1
      irfft:
        planner_effort: FFTW_MEASURE
        threads: 1
      rfft:
        planner_effort: FFTW_MEASURE
        threads: 1
    matrix:
      block:
        assemble: csc
        permc_spec: COLAMD
        use_scipy: true
      sparse:
        diags: csc
        matvec: csr
        permc_spec: COLAMD
        solve: csc
    optimization:
      mode: cython
      verbose: false
    transforms:
      kind:
        chebyshev: fast
        chebyshevu: fast
        fourier: fast
        hermite: vandermonde
        jacobi: recursive
        laguerre: vandermonde
        legendre: recursive
        ultraspherical: recursive

The `basisvectors` can be used to choose `covariant` instead of
`normal` basis vectors. The matrix options decide which scipy
sparse format to use for the sparse computations that make use of them.
The `optimization` can be either `cython`_ or `numba`_, which is used
to speed up some routines. The `fftw` setting allows to tweak the
planning or the use of threads for FFTs. The `bases` configuration for
jacobi can set `mode` to `mpmath` to enable the use of mpmath for
some routines instead of numpy.

Additional dependencies
-----------------------

For storing and retrieving data you need either `HDF5`_ or `netCDF4`_, compiled
with support for MPI (see :ref:`Postprocessing`). Both `HDF5`_  and `netCDF4`_
are already available with parallel support on `conda-forge`_, and, if they were
not installed at the same time as shenfun, they can be installed as

::

    conda install -c conda-forge h5py=*=mpi* netcdf4=*=mpi*

Note that parallel HDF5 and NetCDF4 often are available as modules on
supercomputers. Otherwise, see the respective packages for how to install
with support for MPI.

Some of the plots in the Demos are created using the matplotlib_ library. Matplotlib is not a required dependency, but it may be easily installed from conda using

::

    conda install matplotlib

Test installation
-----------------

After installing (from source) it may be a good idea to run all the tests
located in the `tests <https://github.com/spectralDNS/shenfun/tree/master/tests>`_
folder. The tests are run with `pytest <https://docs.pytest.org/en/latest/>`_
from the main directory of the source code

::

    python -m pytest tests/

However, note that for conda you need to install pytest into the correct
environment as well. A common mistake is to run a version of pytest that has
already been installed in a different conda environment, perhaps using a
different Python version.

The tests are run automatically on every commit to github, see

.. image:: https://dev.azure.com/spectralDNS/shenfun/_apis/build/status/spectralDNS.shenfun?branchName=master
    :target: https://dev.azure.com/spectralDNS/shenfun
.. image:: https://github.com/spectralDNS/shenfun/workflows/github-CI/badge.svg?branch=master
    :target: https://github.com/spectralDNS/shenfun

.. _github: https://github.com/spectralDNS/shenfun
.. _mpi4py-fft: https://bitbucket.org/mpi4py/mpi4py-fft
.. _mpi4py: https://bitbucket.org/mpi4py/mpi4py
.. _cython: http://cython.org
.. _spectralDNS channel: https://anaconda.org/spectralDNS
.. _conda: https://conda.io/docs/
.. _conda-forge: https://conda-forge.org
.. _FFTW: http://www.fftw.org
.. _pip: https://pypi.org/project/pip/
.. _HDF5: https://www.hdfgroup.org
.. _netCDF4: http://unidata.github.io/netcdf4-python/
.. _h5py: https://www.h5py.org
.. _matplotlib: https://matplotlib.org
.. _mpich: https://www.mpich.org
.. _openmpi: https://www.open-mpi.org
.. _numpy: https://www.numpy.org
.. _numba: https://www.numba.org
.. _sympy: https://www.sympy.org
.. _scipy: https://www.scipy.org
.. _conda-build: https://conda.io/docs/commands/build/conda-build.html
.. _pypi: https://pypi.org/project/shenfun/
.. _pyyaml: https://pypi.org/project/PyYAML/
