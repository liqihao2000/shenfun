"""
Module for implementation of the :class:`.TensorProductSpace` class and
related methods.
"""
import copy
from numbers import Number
import sympy as sp
import numpy as np
from mpi4py_fft.mpifft import Transform, PFFT
from mpi4py_fft.pencil import Subcomm, Pencil
from mpi4py import MPI
from shenfun import config
from shenfun.fourier.bases import R2C, C2C
from shenfun.utilities import apply_mask
from shenfun.forms.arguments import Function, Array
from shenfun.optimization.cython import evaluate
from shenfun.spectralbase import slicedict, islicedict, SpectralBase
from shenfun.coordinates import Coordinates

comm = MPI.COMM_WORLD

__all__ = ('TensorProductSpace', 'VectorSpace', 'TensorSpace',
           'CompositeSpace', 'Convolve')

@staticmethod
def _get_kind(xfftn, kind):
    try:
        family = xfftn.func.func.__self__.family()
        kind = {} if kind is None else kind
        return kind.get(family, config['transforms']['kind'][family])
    except:
        return None

@staticmethod
def _get_mesh(xfftn, mesh):
    axis = xfftn.func.func.__self__.axis
    if isinstance(mesh, TensorProductSpace):
        return mesh.bases[axis]
    return mesh

# Monkey-patch
Transform._get_kind = _get_kind
Transform._get_mesh = _get_mesh

class BackwardTransform(Transform):
    def __call__(self, input_array=None, output_array=None, kind=None, mesh=None, **kw):
        """Compute backward transform

        Parameters
        ----------
        input_array : array, optional
        output_array : array, optional
        kind : dict, optional
            Alternatives for implementation. Can be used to overload
            config['transforms']['kind'], with a dictionary with family as key
            and values either one of the strings
            - 'fast'
            - 'recursive'
            - 'vandermonde'
            For example kind={'chebyshev': 'vandermonde'}

        mesh : str or functionspace, optional
            - 'quadrature' - use quadrature mesh
            - 'uniform' - use uniform mesh
            - instance of :class:`.TensorProductSpace` or :class:`.CompositeSpace`
              Use Vandermonde and the quadrature mesh of this space.
              The space must have the same underlying mesh distribution as self

        kw : dict
            parameters to serial transforms
            Note in particular that the keyword ``'normalize'=True/False`` can be
            used to turn normalization on or off. Default is to enable
            normalization for forward transforms and disable it for backward.

        Note
        ----
        If input_array/output_array are not given, then use predefined arrays
        as planned with serial transform object ``_xfftn``.

        """

        if input_array is not None:
            self.input_array[...] = input_array

        for i in range(len(self._transfer)):
            self._xfftn[i](kind=self._get_kind(self._xfftn[i], kind), mesh=self._get_mesh(self._xfftn[i], mesh), **kw)
            arrayA = self._xfftn[i].output_array
            arrayB = self._xfftn[i+1].input_array
            self._transfer[i](arrayA, arrayB)
        self._xfftn[-1](kind=self._get_kind(self._xfftn[-1], kind), mesh=self._get_mesh(self._xfftn[-1], mesh), **kw)

        if output_array is not None:
            output_array[...] = self.output_array
            return output_array
        else:
            return self.output_array

class ScalarTransform(Transform):
    """Class for performing the scalar product in parallel

    Parameters
    ----------
    xfftn : list of serial transform objects
    transfer : list of global redistribution objects
    pencil : list of two pencil objects
        The two pencils represent the input and final output configuration of
        the distributed global arrays
    T : :class:`.TensorProductSpace`
    """
    def __init__(self, xfftn, transfer, pencil, T=None):
        assert len(xfftn) == len(transfer) + 1 and len(pencil) == 2
        assert T is not None
        self._xfftn = tuple(xfftn)
        self._transfer = tuple(transfer)
        self._pencil = tuple(pencil)
        self._T = T

    @property
    def sg(self):
        """Return scaling factors of Transform"""
        return self._T.coors.get_sqrt_det_g()

    @property
    def local_mesh(self):
        return self._T.local_mesh(True)

    def get_measured_input_array(self):
        dx = self.sg
        mesh = self.local_mesh
        if dx == 1:
            return

        if isinstance(dx, Number):
            self.input_array[:] = self.input_array*dx
            return

        sym0 = dx.free_symbols
        m = []
        for sym in sym0:
            j = 'xyzrs'.index(str(sym))
            m.append(mesh[j])
        xj = sp.lambdify(tuple(sym0), dx)(*m)
        self.input_array[...] = self.input_array*xj
        return

    def __call__(self, input_array=None, output_array=None, kind=None, **kw):
        """Compute scalar product

        Parameters
        ----------
        input_array : array, optional
        output_array : array, optional
        kind : dict, optional
            Alternatives for implementation. Can be used to overload
            config['transforms']['kind'], with a dictionary with family as key
            and values either one of the strings
            - 'fast'
            - 'recursive'
            - 'vandermonde'
            For example kind={'chebyshev': 'vandermonde'}
        kw : dict
            parameters to serial transforms
            Note in particular that the keyword ``'normalize'=True/False`` can be
            used to turn normalization on or off. Default is to enable
            normalization for forward transforms and disable it for backward.

        Note
        ----
        If input_array/output_array are not given, then use predefined arrays
        as planned with serial transform object ``_xfftn``.

        """
        if input_array is not None:
            self.input_array[...] = input_array

        if not self._T.coors.is_cartesian:
            self.get_measured_input_array()

        for i in range(len(self._transfer)):
            self._xfftn[i](kind=self._get_kind(self._xfftn[i], kind), **kw)
            arrayA = self._xfftn[i].output_array
            arrayB = self._xfftn[i+1].input_array
            self._transfer[i](arrayA, arrayB)
        self._xfftn[-1](kind=self._get_kind(self._xfftn[-1], kind), **kw)

        if output_array is not None:
            output_array[...] = self.output_array
            return output_array
        else:
            return self.output_array


class ForwardTransform(ScalarTransform):
    """Class for performing the forward transform in parallel

    Parameters
    ----------
    xfftn : list of serial transform objects
    transfer : list of global redistribution objects
    pencil : list of two pencil objects
        The two pencils represent the input and final output configuration of
        the distributed global arrays
    T : :class:`.TensorProductSpace`
    """
    def __call__(self, input_array=None, output_array=None, kind=None, **kw):
        """Compute forward transform

        Parameters
        ----------
        input_array : array, optional
        output_array : array, optional
        kind : dict, optional
            Alternatives for implementation. Can be used to overload
            config['transforms']['kind'], with a dictionary with family as key
            and values either one of the strings
            - 'fast'
            - 'recursive'
            - 'vandermonde'
            For example kind={'chebyshev': 'vandermonde'}
        kw : dict
            parameters to serial transforms
            Note in particular that the keyword ``'normalize'=True/False`` can be
            used to turn normalization on or off. Default is to enable
            normalization for forward transforms and disable it for backward.

        Note
        ----
        If input_array/output_array are not given, then use predefined arrays
        as planned with serial transform object ``_xfftn``.

        """
        if input_array is not None:
            self.input_array[...] = input_array

        if not self._T.coors.is_cartesian:
            self.get_measured_input_array()

        if len(self._T.get_nonhomogeneous_axes()) > 1:
            from shenfun import la, TestFunction, TrialFunction, inner
            assert self._T.dimensions == 2, 'Two inhomogeneous boundary directions only implemented for 2D'
            u = TrialFunction(self._T)
            v = TestFunction(self._T)
            B = inner(u, v, kind=kind)
            b = inner(v, input_array, kind=kind)
            sol = la.Solver2D(B)
            self.output_array[:] = sol(b)
        else:
            for i in range(len(self._transfer)):
                self._xfftn[i](kind=self._get_kind(self._xfftn[i], kind), **kw)
                arrayA = self._xfftn[i].output_array
                arrayB = self._xfftn[i+1].input_array
                self._transfer[i](arrayA, arrayB)
            self._xfftn[-1](kind=self._get_kind(self._xfftn[-1], kind), **kw)

        if output_array is not None:
            output_array[...] = self.output_array
            return output_array
        else:
            return self.output_array


class TensorProductSpace(PFFT):
    """Class for multidimensional tensorproductspaces.

    The tensorproductspaces are created as Cartesian products from a set of 1D
    bases. The 1D bases are subclassed instances of the :class:`.SpectralBase`
    class.

    Parameters
    ----------
    comm : MPI communicator
    bases : list
        List of 1D bases (:class:`.SpectralBase`)
    axes : tuple of ints, optional
        A tuple containing the order of which to perform transforms.
        Last item is transformed first. Defaults to range(len(bases))
    dtype : data-type, optional
        Type of input data in real physical space. If not provided it
        will be inferred from the bases.
    slab : bool, optional
        Use 1D slab decomposition.
    collapse_fourier : bool, optional
        Collapse axes for Fourier bases if possible
    backward_from_pencil : False or Pencil
        In case of Pencil configure the transform by starting from the
        Pencil distribution in spectral space. This is primarily intended
        for a padded space, where the spectral distribution must be
        equal to the non-padded space.
    coordinates : 2- or 3-tuple, optional
        Map for curvilinear coordinatesystem.
        First tuple are the coordinate variables in the new coordinate system
        Second tuple are the Cartesian coordinates as functions of the
        variables in the first tuple. Example::

            psi = (theta, r) = sp.symbols('x,y', real=True, positive=True)
            rv = (r*sp.cos(theta), r*sp.sin(theta))

        where psi and rv are the first and second tuples, respectively.
        If a third item is provided with the tuple, then this third item
        is used as an additional assumption. For example, it is necessary
        to provide the assumption ``sympy.Q.positive(sympy.sin(theta))``,
        such that sympy can evaluate ``sqrt(sympy.sin(theta)**2)`` to
        ``sympy.sin(theta)`` and not ``Abs(sympy.sin(theta))``. Different
        coordinates may require different assumptions to help sympy when
        computing basis functions etc. See :class:`~shenfun.coordinates.Coordinates`.
    modify_spaces_inplace : bool, optional
        Whether or not a copy should be made of the input functionspaces.
        If True, then the input spaces will be modified inplace.
    kw : dict, optional
        Dictionary that can be used to plan transforms. Input to method
        ``plan`` for the bases.

    """
    def __init__(self, comm, bases, axes=None, dtype=None, slab=False,
                 collapse_fourier=False, backward_from_pencil=False,
                 coordinates=None, modify_spaces_inplace=False, **kw):
        # Note do not call __init__ of super
        self.comm = comm
        self.bases = bases
        if not modify_spaces_inplace:
            self.bases = tuple([base.get_unplanned() for base in bases])

        coors = coordinates
        if coors is None:
            psi = sp.symbols('x,y,z,r,s', real=True)
            coors = (psi[:len(bases)],)*2

        self.coors = Coordinates(*coors)
        self.hi = self.coors.hi
        self.sg = self.coors.sg
        shape = list(self.global_shape())
        self.axes = axes
        assert shape
        #assert min(shape) > 0
        if min(shape) == 0:
            self.subcomm = comm
            self._dtype = dtype
            return

        if axes is not None:
            axes = list(axes) if not isinstance(axes, int) else [axes]
        else:
            axes = list(range(len(shape)))

        for i, ax in enumerate(axes):
            if isinstance(ax, (int, np.integer)):
                if ax < 0:
                    ax += len(shape)
                axes[i] = (ax,)
            else:
                assert isinstance(ax, (tuple, list))
                ax = list(ax)
                for j, a in enumerate(ax):
                    assert isinstance(a, int)
                    if a < 0:
                        a += len(shape)
                        ax[j] = a
                axes[i] = ax
            assert min(axes[i]) >= 0
            assert max(axes[i]) < len(shape)
            assert 0 < len(axes[i]) <= len(shape)
            assert sorted(axes[i]) == sorted(set(axes[i]))

        if dtype is None:
            dtype = complex if isinstance(self.bases[axes[-1][-1]], C2C) else float

        dtype = np.dtype(dtype)
        assert dtype.char in 'fdgFDG'

        self.xfftn = []
        self.transfer = []
        self.pencil = [None, None]
        for axis, base in enumerate(self.bases):
            base.tensorproductspace = self
            base.axis = axis
            base.sl = slicedict(axis=axis, dimensions=len(self.bases))
            base.si = islicedict(axis=axis, dimensions=len(self.bases))

        self.axes = axes
        if not backward_from_pencil:
            if isinstance(self.bases[axes[-1][-1]], C2C):
                assert np.dtype(dtype).char in 'FDG'
            elif isinstance(self.bases[axes[-1][-1]], R2C):
                assert np.dtype(dtype).char in 'fdg'

            if isinstance(comm, tuple):
                assert slab is False
                assert len(comm) == len(shape)
                assert comm[axes[-1][-1]].Get_size() == 1
                self.subcomm = comm
            else:
                if slab:
                    axis = (axes[-1][-1] + 1) % len(shape)
                    dims = [1] * len(shape)
                    dims[axis] = comm.Get_size()
                else:
                    dims = [0] * len(shape)
                    for ax in axes[-1]:
                        dims[ax] = 1
                self.subcomm = Subcomm(comm, dims)

            # At this points axes is a tuple of tuples of length one.
            # Try to collapse some Fourier transforms into one.
            if np.any([abs(base.padding_factor - 1.0) > 1e-6 for base in self.bases]):
                collapse_fourier = False
            if collapse_fourier:
                F = lambda ax: self.bases[ax].family() == 'fourier' and self.subcomm[ax].Get_size() == 1
                axis = self.axes[-1][-1]
                groups = [list(self.axes[-1])]
                F0 = F(axis)
                for ax in reversed(self.axes[:-1]):
                    axis = ax[-1]
                    if F0 and F(axis):
                        groups[0].insert(0, axis)
                    else:
                        groups.insert(0, list(ax))
                    F0 = F(axis)
                self.axes = groups
            self.axes = tuple(map(tuple, self.axes))

            # Configure all transforms
            axes = self.axes[-1]
            pencil = Pencil(self.subcomm, shape, axes[-1])
            self.xfftn.append(self.bases[axes[-1]])
            self.xfftn[-1].plan(pencil.subshape, axes, dtype, kw)
            self.pencil[0] = pencilA = pencil
            if not (shape[axes[-1]] == self.xfftn[-1].forward.output_array.shape[axes[-1]] and
                    self.xfftn[-1].forward.input_array.dtype == self.xfftn[-1].forward.output_array.dtype):
                dtype = self.xfftn[-1].forward.output_array.dtype
                shape[axes[-1]] = self.xfftn[-1].forward.output_array.shape[axes[-1]]
                pencilA = Pencil(self.subcomm, shape, axes[-1])

            for i, axes in enumerate(reversed(self.axes[:-1])):
                pencilB = pencilA.pencil(axes[-1])
                transAB = pencilA.transfer(pencilB, dtype)
                xfftn = self.bases[axes[-1]]
                xfftn.plan(pencilB.subshape, axes, dtype, kw)
                self.xfftn.append(xfftn)
                self.transfer.append(transAB)
                pencilA = pencilB
                if not (shape[axes[-1]] == xfftn.forward.output_array.shape[axes[-1]] and
                        xfftn.forward.input_array.dtype == xfftn.forward.output_array.dtype):
                    dtype = xfftn.forward.output_array.dtype
                    shape[axes[-1]] = xfftn.forward.output_array.shape[axes[-1]]
                    pencilA = Pencil(pencilB.subcomm, shape, axes[-1])

            self.pencil[1] = pencilA

            self.forward = ForwardTransform(
                [o.forward for o in self.xfftn],
                [o.forward for o in self.transfer],
                self.pencil, self)
            self.backward = BackwardTransform(
                [o.backward for o in self.xfftn[::-1]],
                [o.backward for o in self.transfer[::-1]],
                self.pencil[::-1])
            self.scalar_product = ScalarTransform(
                [o.scalar_product for o in self.xfftn],
                [o.forward for o in self.transfer],
                self.pencil, self)

        else:
            self.configure_backwards(backward_from_pencil, dtype, kw)

        for i, base in enumerate(self.bases):
            base.axis = i
            if base.has_nonhomogeneous_bcs:
                base.bc.set_tensor_bcs(base, self)

    def configure_backwards(self, pencil, dtype, kw):
        """Configure transforms starting from spectral space

        Parameters
        ----------
        pencil : :class:`.Pencil`
            The distribution in spectral space
        dtype : Numpy dtype
            The type of data in spectral space
        kw : dict
            Any parameters for planning transforms

        Note
        ----
        To ensure the same distribution in spectral space, the padded
        space must be configured by moving from the spectral space towards the
        physical space. The distribution does not have to agree in physical
        space, because the padding is done only in the spectral.
        """
        shape = list(self.global_shape(True))
        axes = self.axes[0]
        xfftn = self.bases[axes[-1]]
        self.xfftn.append(xfftn)
        self.subcomm = pencil.subcomm
        subshape = list(pencil.subshape)
        if isinstance(xfftn, R2C):
            subshape[axes[-1]] = int(np.floor(xfftn.N*xfftn.padding_factor))
            dtype = np.dtype(dtype.char.lower())
        else:
            subshape[axes[-1]] = int(np.floor(subshape[axes[-1]]*xfftn.padding_factor))
        self.xfftn[-1].plan(subshape, axes, dtype, kw)
        if not (shape[axes[-1]] == self.xfftn[-1].forward.input_array.shape[axes[-1]] and
                self.xfftn[-1].forward.input_array.dtype == self.xfftn[-1].forward.output_array.dtype):
            dtype = self.xfftn[-1].forward.input_array.dtype
            shape[axes[-1]] = self.xfftn[-1].forward.input_array.shape[axes[-1]]
        pencilA = Pencil(pencil.subcomm, shape, axes[0])
        self.pencil[0] = pencilA
        for axes in self.axes[1:]:
            pencilB = pencilA.pencil(axes[-1])
            transBA = pencilA.transfer(pencilB, dtype)
            xfftn = self.bases[axes[-1]]
            subshape = list(pencilB.subshape)
            if isinstance(xfftn, R2C):
                subshape[axes[-1]] = int(np.floor(xfftn.N*xfftn.padding_factor))
                dtype = np.dtype(dtype.char.lower())
            else:
                subshape[axes[-1]] = int(np.floor(subshape[axes[-1]]*xfftn.padding_factor))
            xfftn.plan(subshape, axes, dtype, kw)
            self.xfftn.append(xfftn)
            self.transfer.append(transBA)
            pencilA = pencilB
            if not (shape[axes[-1]] == xfftn.forward.input_array.shape[axes[-1]] and
                    xfftn.forward.input_array.dtype == xfftn.forward.output_array.dtype):
                dtype = xfftn.forward.input_array.dtype
                shape[axes[-1]] = xfftn.forward.input_array.shape[axes[-1]]
                pencilA = Pencil(pencilB.subcomm, shape, axes[-1])

        self.pencil[1] = pencilA

        self.backward = BackwardTransform(
            [o.backward for o in self.xfftn],
            [o.forward for o in self.transfer],
            self.pencil)
        self.forward = ForwardTransform(
            [o.forward for o in self.xfftn[::-1]],
            [o.backward for o in self.transfer[::-1]],
            self.pencil[::-1], self)
        self.scalar_product = ScalarTransform(
            [o.scalar_product for o in self.xfftn[::-1]],
            [o.forward for o in self.transfer[::-1]],
            self.pencil[::-1], self)

    def get_dealiased(self, padding_factor=1.5, dealias_direct=False):
        """Return space (otherwise as self) to be used for dealiasing

        Parameters
        ----------
        padding_factor : number or tuple, optional
            Scale the number of quadrature points in self with this number, or
            tuple of numbers, one for each dimension.
        dealias_direct : bool, optional
            Used only if ``padding_factor=1``. Sets the 1/3 highest frequencies
            to zeros.
        """
        if padding_factor == 1 and dealias_direct is False:
            return self
        if isinstance(padding_factor, Number):
            padding_factor = (padding_factor,)*len(self)
        elif isinstance(padding_factor, (tuple, list, np.ndarray)):
            assert len(padding_factor) == len(self)
        padded_bases = [base.get_dealiased(padding_factor=padding_factor[axis],
                                           dealias_direct=dealias_direct)
                        for axis, base in enumerate(self.bases)]
        # Need the correct order of the transforms in case reversed somehow
        axes = []
        for ax in self.axes:
            for ai in ax:
                axes.append(ai)
        return TensorProductSpace(self.comm, padded_bases, axes=tuple(axes),
                                  dtype=self.forward.output_array.dtype,
                                  backward_from_pencil=self.forward.output_pencil,
                                  coordinates=self.coors.coordinates)

    def get_refined(self, N):
        """Return space (otherwise as self) refined to new shape

        Parameters
        ----------
        N : Number or sequence of integers
            The global shape of the new space. If N is a number, then
            refine the global shape by multiplying self's global shape by N.
            If N is an array, then N is treated as the new global shape of
            the returned space.
        """
        if isinstance(N, Number):
            N = N*np.array(self.global_shape())
        elif isinstance(N, (tuple, list, np.ndarray)):
            assert len(N) == len(self)
        refined_bases = [base.get_refined(N[axis])
                         for axis, base in enumerate(self.bases)]
        return TensorProductSpace(self.subcomm, refined_bases, axes=self.axes,
                                  dtype=self.dtype(),
                                  coordinates=self.coors.coordinates)

    def get_unplanned(self, tensorproductspace=False, **kwargs):
        """Return unplanned bases otherwise as self. Or return a new
        TensorProductSpace from the unplanned bases.

        Parameters
        ----------
        tensorproductspace : boolean
            if True, then return a new TensorProductSpace
            if False, return only the bases
        kwargs : keyword arguments
            Any arguments used in the creation of the different bases.

        """
        bases = tuple([base.get_unplanned(**kwargs) for base in self.bases])
        if tensorproductspace:
            return TensorProductSpace(comm, bases)
        return bases

    def get_homogeneous(self, **kwargs):
        """Return space with homogeneous boundary conditions, and otherwise
        as self.

        Parameters
        ----------
        kwargs : keyword arguments
            Any arguments used in the creation of the different bases.

        """
        bases = tuple([base.get_homogeneous(**kwargs) for base in self.bases])
        return TensorProductSpace(comm, bases)

    def dtype(self, forward_output=False):
        if hasattr(self, 'forward'):
            return PFFT.dtype(self, forward_output)
        return self._dtype

    def convolve(self, a_hat, b_hat, ab_hat):
        """Convolution of a_hat and b_hat

        Parameters
        ----------
        a_hat : array
            Input array of shape and type as output array from
            self.forward, or instance of :class:`.Function`
        b_hat : array
            Input array of shape and type as output array from
            self.forward, or instance of :class:`.Function`
        ab_hat : array
            Return array of same type and shape as a_hat and b_hat

        Note
        ----
        The return array ab_hat is truncated to the shape of a_hat and b_hat.

        Also note that self should have bases with padding for this method to give
        a convolution without aliasing. The padding is specified when creating
        instances of bases for the :class:`.TensorProductSpace`.

        FIXME Efficiency due to allocation
        """
        a = self.backward.output_array.copy()
        b = self.backward.output_array.copy()
        a = self.backward(a_hat, a)
        b = self.backward(b_hat, b)
        ab_hat = self.forward(a*b, ab_hat)
        return ab_hat

    def eval(self, points, coefficients, output_array=None, method=1):
        """Evaluate Function at points, given expansion coefficients

        Parameters
        ----------
        points : float or array of floats
            Array must be of shape (D, N), for N points in D dimensions
        coefficients : array
            Expansion coefficients, or instance of :class:`.Function`
        output_array : array, optional
            Return array, function values at points
        method : int, optional
            Chooses implementation. The method 0 is a low-memory cython
            version. Using method = 1 (default) leads to a faster cython
            implementation that, on the downside, uses more memory.
            The final, method = 2, is a python implementation.
        """
        if output_array is None:
            output_array = np.zeros(points.shape[1], dtype=self.forward.input_array.dtype)
        else:
            output_array[:] = 0
        if len(self.get_nonperiodic_axes()) > 1:
            method = 1
        assert self.dimensions < 4, 'eval not implemented (yet) for higher dimensions'
        if method == 0:
            return self._eval_lm_cython(points, coefficients, output_array)
        elif method == 1:
            return self._eval_cython(points, coefficients, output_array)
        else:
            return self._eval_python(points, coefficients, output_array)

    def _eval_python(self, points, coefficients, output_array):
        """Evaluate Function at points, given expansion coefficients

        Parameters
        ----------
        points : float or array of floats
        coefficients : array
            Expansion coefficients
        output_array : array
            Return array, function values at points
        """
        P = []
        last_conj_index = -1
        sl = -1
        out = []
        previous_axes = []
        flataxes = []
        for ax in self.axes:
            for a in ax:
                flataxes.append(a)
        for axis in flataxes:
            base = self.bases[axis]
            x = base.map_reference_domain(points[axis])
            D = base.evaluate_basis_all(x=x, argument=1)
            P = D[..., self.local_slice()[axis]]
            if isinstance(base, R2C):
                M = base.N//2+1
                if base.N % 2 == 0:
                    last_conj_index = M-1
                else:
                    last_conj_index = M
                offset = self.local_slice()[axis].start
                sl = offset
                st = self.local_slice()[axis].stop
                if sl == 0:
                    sl = 1
                st = min(last_conj_index, st) - offset
                sl -= offset
                sp = [slice(None), slice(sl, st)]

            if len(out) == 0:
                out = np.tensordot(P, coefficients, (1, axis))
                if isinstance(base, R2C):
                    ss = [slice(None)]*len(self)
                    ss[axis] = slice(sl, st)
                    out += np.conj(np.tensordot(P[tuple(sp)], coefficients[tuple(ss)], (1, axis)))
                    out = out.real

            else:
                k = np.count_nonzero([m < axis for m in previous_axes])
                if len(self) == 2:
                    if not isinstance(base, R2C):
                        out2 = np.sum(P*out, axis=-1)
                    else:
                        sp = tuple(sp)
                        out2 = np.sum(P.real*out.real - P.imag*out.imag, axis=-1)
                        out2 += np.sum(np.conj(P[sp].real*out[sp].real - P[sp].imag*out[sp].imag), axis=-1)

                elif len(self) == 3:
                    sx = [slice(None)]*out.ndim
                    sd = [slice(None)]*out.ndim
                    if len(out.shape) == 3:
                        kk = 1 if axis-k == 1 else 2
                        sx[kk] = np.newaxis
                    if not isinstance(base, R2C):
                        out2 = np.sum(P[tuple(sx)]*out, axis=1+axis-k)
                    else:
                        out2 = np.sum(P[tuple(sx)].real*out.real - P[tuple(sx)].imag*out.imag, axis=1+axis-k)
                        sx[1+axis-k] = slice(sl, st)
                        sd[1+axis-k] = slice(sl, st)
                        sx = tuple(sx)
                        sd = tuple(sd)
                        out2 += np.sum(np.conj(P[sx].real*out[sd].real - P[sx].imag*out[sd].imag), axis=1+axis-k)
                out = out2
            previous_axes.append(axis)
        output_array[:] = out
        output_array = comm.allreduce(output_array)
        return output_array


    def _eval_lm_cython(self, points, coefficients, output_array):
        """Evaluate Function at points, given expansion coefficients

        Parameters
        ----------
        points : float or array of floats
        coefficients : array
            Expansion coefficients
        output_array : array
            Return array, function values at points
        """
        r2c = -1
        last_conj_index = -1
        sl = -1
        x = []
        w = []
        for base in self:
            axis = base.axis
            if isinstance(base, R2C):
                r2c = axis
                M = base.N//2+1
                if base.N % 2 == 0:
                    last_conj_index = M-1
                else:
                    last_conj_index = M
                sl = self.local_slice()[axis].start
            x.append(base.map_reference_domain(points[axis]))
            w.append(base.wavenumbers(bcast=False)[self.local_slice()[axis]].astype(float))

        if len(self) == 2:
            output_array = evaluate.evaluate_lm_2D(list(self.bases), output_array, coefficients, x[0], x[1], w[0], w[1], r2c, last_conj_index, sl)

        elif len(self) == 3:
            output_array = evaluate.evaluate_lm_3D(list(self.bases), output_array, coefficients, x[0], x[1], x[2], w[0], w[1], w[2], r2c, last_conj_index, sl)

        output_array = np.atleast_1d(output_array)
        output_array = comm.allreduce(output_array)
        return output_array

    def _eval_cython(self, points, coefficients, output_array):
        """Evaluate Function at points, given expansion coefficients

        Parameters
        ----------
        points : float or array of floats
        coefficients : array
            Expansion coefficients
        output_array : array
            Return array, function values at points
        """
        P = []
        r2c = -1
        last_conj_index = -1
        sl = -1
        for base in self:
            axis = base.axis
            x = base.map_reference_domain(points[axis])
            D = base.evaluate_basis_all(x=x, argument=1)
            P.append(D[..., self.local_slice()[axis]])
            if isinstance(base, R2C):
                r2c = axis
                M = base.N//2+1
                if base.N % 2 == 0:
                    last_conj_index = M-1
                else:
                    last_conj_index = M
                sl = self.local_slice()[axis].start
        if len(self) == 2:
            output_array = evaluate.evaluate_2D(output_array, coefficients, P, r2c, last_conj_index, sl)

        elif len(self) == 3:
            output_array = evaluate.evaluate_3D(output_array, coefficients, P, r2c, last_conj_index, sl)

        output_array = np.atleast_1d(output_array)
        output_array = comm.allreduce(output_array)
        return output_array

    def wavenumbers(self, scaled=False, eliminate_highest_freq=False):
        """Return list of wavenumbers

        Parameters
        ----------
        scaled : bool, optional
            Scale wavenumbers with size of box
        eliminate_highest_freq : bool, optional
            Set Nyquist frequency to zero for evenly shaped axes of Fourier
            bases.

        """
        K = []
        for base in self:
            K.append(base.wavenumbers(scaled=scaled,
                                      eliminate_highest_freq=eliminate_highest_freq))
        return K

    def local_wavenumbers(self, broadcast=False, scaled=False,
                          eliminate_highest_freq=False):
        """Return list of local wavenumbers

        Parameters
        ----------
        broadcast : bool, optional
            Broadcast returned wavenumber arrays to actual
            dimensions of :class:`.TensorProductSpace`
        scaled : bool, optional
            Scale wavenumbers with size of box
        eliminate_highest_freq : bool, optional
            Set Nyquist frequency to zero for evenly shaped axes of Fourier
            bases

        """
        k = self.wavenumbers(scaled=scaled, eliminate_highest_freq=eliminate_highest_freq)
        lk = []
        for axis, (n, s) in enumerate(zip(k, self.local_slice(True))):
            ss = [slice(None)]*len(k)
            ss[axis] = s
            lk.append(n[tuple(ss)])
        if broadcast is True:
            return [np.broadcast_to(m, self.shape()) for m in lk]
        return lk

    def mesh(self, map_true_domain=True, kind='quadrature'):
        """Return list of 1D physical meshes for each dimension of
        :class:`.TensorProductSpace`

        Parameters
        ----------
        map_true_domain : bool, optional
            Whether or not to map points to true domain
        kind : str, optional

            - 'quadrature' - Use quadrature mesh
            - 'uniform' - Use uniform mesh
        """
        X = []
        for base in self:
            X.append(base.mesh(bcast=True, map_true_domain=map_true_domain, kind=kind))
        return X

    def local_mesh(self, bcast=False, map_true_domain=True, kind='quadrature'):
        """Return list of local 1D physical meshes for each dimension of
        :class:`.TensorProductSpace`

        Parameters
        ----------
        bcast : bool, optional
            Broadcast each 1D mesh to real shape of
            :class:`.TensorProductSpace`
        map_true_domain : bool, optional
            Whether or not to map points to true domain
        kind : str, optional

            - 'quadrature' - Use quadrature mesh
            - 'uniform' - Use uniform mesh
        """
        mesh = self.mesh(map_true_domain=map_true_domain, kind=kind)
        lm = []
        for axis, (n, s) in enumerate(zip(mesh, self.local_slice(False))):
            ss = [slice(None)]*len(mesh)
            ss[axis] = s
            lm.append(n[tuple(ss)])
        if bcast is True:
            return [np.broadcast_to(m, self.shape(False)) for m in lm]
        return lm

    def local_cartesian_mesh(self, map_true_domain=True, kind='quadrature'):
        """Return local Cartesian mesh

        Parameters
        ----------
        map_true_domain : bool, optional
            Whether or not to map points to true domain
        kind : str, optional

            - 'quadrature' - Use quadrature mesh
            - 'uniform' - Use uniform mesh
        """
        X = self.local_mesh(bcast=True, map_true_domain=map_true_domain, kind=kind)
        xx = []
        psi = self.coors.coordinates[0]
        for rv in self.coors.coordinates[1]:
            xx.append(sp.lambdify(psi, rv)(*X))
        return xx

    def cartesian_mesh(self, map_true_domain=True, kind='quadrature'):
        """Return Cartesian mesh

        Parameters
        ----------
        map_true_domain : bool, optional
            Whether or not to map points to true domain
        kind : str, optional

            - 'quadrature' - Use quadrature mesh
            - 'uniform' - Use uniform mesh
        """
        X = self.mesh(map_true_domain=map_true_domain, kind=kind)
        xx = []
        psi = self.coors.coordinates[0]
        for rv in self.coors.coordinates[1]:
            xx.append(sp.lambdify(psi, rv)(*X))
        return xx

    def dim(self):
        """Return dimension of ``self`` (degrees of freedom)"""
        return np.prod(self.dims())

    def dims(self):
        """Return dimensions (degrees of freedom) of all bases in ``self``"""
        return tuple([base.dim() for base in self])

    def size(self, forward_output=False):
        """Return number of elements in :class:`.TensorProductSpace`"""
        return np.prod(self.shape(forward_output))

    def slice(self):
        """The slices of dofs for each dimension"""
        return tuple(base.slice() for base in self.bases)

    @property
    def use_fixed_gauge(self):
        """Return True if :class:`.TensorProductSpace` contains only spaces
        with Neumann boundary conditions.
        """
        for base in self.bases:
            if base.boundary_condition() != 'Neumann':
                # Any other boundary condition triggers False
                # It is only Neumann bases that requires a gauge
                return False
        return True

    def global_shape(self, forward_output=False):
        """Return global shape of arrays for :class:`.TensorProductSpace`

        Parameters
        ----------
        forward_output : bool, optional
            If True then return shape of an array that is the result of a
            forward transform. If False then return shape of physical
            space, i.e., the input to a forward transform.

        """
        return tuple([base.shape(forward_output) for base in self])

    @property
    def domains(self):
        """Return domains for all directions
        """
        return tuple([base.domain for base in self])

    @property
    def volume(self):
        """Return computational volume
        """
        vol = 1
        for d in self.domains:
            vol *= (d[1]-d[0])
        return vol

    def mask_nyquist(self, u_hat, mask=None):
        """Return :class:`.Function` ``u_hat`` with zero Nyquist coefficients

        Parameters
        ----------
        u_hat : array
            :class:`.Function` to be masked
        mask : array or None, optional
            mask array, if not provided then get the mask by calling
            :func:`get_mask_nyquist`
        """
        if mask is None:
            mask = self.get_mask_nyquist()
        u_hat = apply_mask(u_hat, mask)
        return u_hat

    def get_mask_nyquist(self):
        """Return an array with zeros for Nyquist coefficients and one otherwise"""
        k = []
        do_mask = False
        for base in self:
            if base.family() == 'fourier':
                k.append(base.get_mask_nyquist(bcast=True))
                if base.N % 2 == 0:
                    do_mask = True
            else:
                k.append(None)

        if not do_mask:
            return None

        lk = []
        for axis, (n, s) in enumerate(zip(k, self.local_slice(True))):
            if n is None:
                lk.append(1)
            else:
                ss = [slice(None)]*len(k)
                ss[axis] = s
                lk.append(n[tuple(ss)])

        mask = 1
        for ll in lk:
            mask = mask * ll
        return mask

    def get_measured_array(self, u):
        """Weigh Array ``u`` with integral measure

        Parameters
        ----------
        u : Array
        """
        dx = self.coors.get_sqrt_det_g()
        mesh = self.local_mesh(True)
        if dx == 1:
            return u

        if isinstance(dx, Number):
            u *= dx
            return u

        sym0 = dx.free_symbols
        m = []
        for sym in sym0:
            j = 'xyzrs'.index(str(sym))
            m.append(mesh[j])
        xj = sp.lambdify(tuple(sym0), dx)(*m)
        u *= xj
        return u

    def __iter__(self):
        return iter(self.bases)

    @property
    def rank(self):
        """Return tensor rank of :class:`.TensorProductSpace`"""
        return 0

    @property
    def tensor_rank(self):
        return 0

    def __len__(self):
        """Return dimension of :class:`.TensorProductSpace`"""
        return len(self.bases)

    def num_components(self):
        """Return number of scalar spaces in :class:`.TensorProductSpace`"""
        return 1

    @property
    def dimensions(self):
        """Return dimension of :class:`.TensorProductSpace`"""
        return self.__len__()

    def get_nonperiodic_axes(self):
        axes = []
        for axis, base in enumerate(self):
            if not base.family() == 'fourier':
                axes.append(axis)
        return np.array(axes)

    def get_nondiagonal_axes(self):
        # Return list of axes that **may** contain non-diagonal matrices
        axes = []
        if isinstance(self.coors.hi.prod(), sp.Expr):
            x = 'xyzrs'
            sym = self.coors.hi.prod().free_symbols
            msaxes = set()
            for s in sym:
                if str(s) in x:
                    msaxes.add(x.index(str(s)))

        for axis, base in enumerate(self):
            if not base.family() == 'fourier':
                axes.append(axis)
            if base.family() == 'fourier' and (axis in msaxes) and base.N > 1:
                axes.append(axis)
        return np.array(axes)

    def get_diagonal_axes(self):
        return np.setxor1d(self.get_nondiagonal_axes(), range(self.dimensions)).astype(int)

    def get_nonhomogeneous_axes(self):
        axes = []
        for axis, base in enumerate(self):
            if base.has_nonhomogeneous_bcs:
                axes.append(axis)
        return np.array(axes)

    @property
    def is_orthogonal(self):
        ortho = True
        for base in self.bases:
            ortho *= base.is_orthogonal
        return ortho

    @property
    def is_padded(self):
        padded = True
        for base in self.bases:
            padded *= base.is_padded
        return padded

    def get_orthogonal(self):
        """Return tensor product space using the orthogonal basis for all directions"""
        ortho = []
        for base in self.bases:
            ortho.append(base.get_orthogonal())
        return TensorProductSpace(self.subcomm, ortho, axes=self.axes,
                                  dtype=self.forward.input_array.dtype,
                                  coordinates=self.coors.coordinates)

    def get_testspace(self, kind='Galerkin', **kwargs):
        r"""Return appropriate test space

        Parameters
        ----------
        kind : str, optional

            - 'Galerkin' - or 'G'
            - 'Petrov-Galerkin' - or 'PG'

            If kind = 'Galerkin', then return self, corresponding to a regular
            Galerkin method.

            If kind = 'Petrov-Galerkin' or 'PG', then return a test space that makes
            use of the testbases `Phi1`, `Phi2`, `Phi3` or `Phi4`, where the choice
            is made based on the number of boundary conditions in self. One boundary
            condition uses `Phi1`, two uses `Phi2` etc.

        kwargs : keyword arguments, optional
            Any other keyword arguments used in the creation of the test bases.

        Note
        ----
        Petrov-Galerkin is only possible for Jacobi polynomials. If mixed with,
        e.g., Fourier spaces, then regular Galerkin is used for the non-polynomial
        bases.

        Returns
        -------
        Instance of :class:`.TensorProductSpace`
        """
        test = []
        for base in self.bases:
            test.append(base.get_testspace(kind=kind, **kwargs))
        return TensorProductSpace(self.subcomm, test, axes=self.axes,
                                  dtype=self.forward.input_array.dtype,
                                  coordinates=self.coors.coordinates)

    def get_adaptive(self, fun=None, reltol=1e-12, abstol=1e-15):
        """Return space (otherwise as self) with number of quadrature points
        determined by fitting `fun`

        Returns
        -------
        :class:`.TensorProductSpace`
            A new space with adaptively found number of quadrature points

        Note
        ----
        Only spaces defined with N=0 quadrature points are adapted, the
        others are kept as is

        Example
        -------
        >>> from shenfun import FunctionSpace, Function, TensorProductSpace, comm
        >>> import sympy as sp
        >>> r, theta = psi = sp.symbols('x,y', real=True, positive=True)
        >>> rv = (r*sp.cos(theta), r*sp.sin(theta))
        >>> B0 = FunctionSpace(0, 'C', domain=(0, 1))
        >>> F0 = FunctionSpace(0, 'F', dtype='d')
        >>> T = TensorProductSpace(comm, (B0, F0), coordinates=(psi, rv))
        >>> u = Function(T, buffer=((1-r)*r)**4*(sp.cos(10*theta)))
        >>> print(u.global_shape)
        (9, 11)

        """
        domains = [base.domain for base in self.bases]
        sym0 = fun.free_symbols
        sd = {str(sym): sym for sym in sym0}
        Tj = []
        for axis, base in enumerate(self.bases):
            # First remove all other axes from the function
            otheraxes = list(range(len(self.bases)))
            otheraxes.remove(axis)
            fj = fun.copy()
            bcc = None
            if base.has_nonhomogeneous_bcs:
                bcc = base.bc.bcs.copy()
            for ax in otheraxes:
                fj = fj.subs(sd['xyzrs'[ax]], 0.5*(domains[ax][1]-domains[ax][0]))
            if base.N == 0:
                par = dict(fun=fj, reltol=reltol, abstol=abstol)
                if base.has_nonhomogeneous_bcs:
                    for l, bc in enumerate(bcc):
                        bc = bc.subs(sd['xyzrs'[ax]], 0.5*(domains[ax][1]-domains[ax][0]))
                        bcc[l] = bc
                    par['bc'] = bcc
                Tj.append(base.get_adaptive(**par))
                if base.has_nonhomogeneous_bcs:
                    Tj[-1].bc = BoundaryValues(Tj[-1], bc=base.bc)
            else:
                Tj.append(base)
        return TensorProductSpace(self.subcomm, Tj, axes=self.axes,
                                  coordinates=self.coors.coordinates)

    @property
    def is_composite_space(self):
        return 0

    def __getitem__(self, i):
        """Return instance of base i

        Parameters
        ----------
        i : int
        """
        return self.bases[i]

    def compatible_base(self, space):
        """Return whether space is compatible with self.

        Parameters
        ----------

        space : :class:`.TensorProductSpace`
            The space compared to

        Note
        ----
        Two spaces are deemed compatible if the underlying bases, along each
        direction, belong to the same family, has the same number of quadrature
        points, and the same quadrature scheme.
        """
        compatible = True
        for base0, base1 in zip(self.bases, space.bases):
            if not hash(base0) == hash(base1):
                compatible = False
                break
        return compatible

    def shape(self, forward_output=True):
        """The local (to each processor) shape of data

        Parameters
        ----------
        forward_output : bool, optional
            Return shape of output array (spectral space) if True, else return
            shape of input array (physical space)
        """
        if forward_output is not True:
            return self.forward.input_pencil.subshape
        return self.forward.output_array.shape

    def _get_ndiag_cum_dofs(self):
        """Return the cumulative sum of degrees of freedom along nondiagonal axes"""
        dims = np.array(self.dims())
        daxes = self.get_diagonal_axes()
        if len(daxes) > 0:
            dims[daxes] = 1
        return np.array([0, np.prod(dims)], dtype=int)

    def _get_ndiag_slices(self, j=()):
        """Return a sequence of slices for non-diagonal axes and integers for diagonal"""
        assert self.num_components() == 1
        sl = (0,)+self.slice()
        daxes = self.get_diagonal_axes()
        if len(daxes) > 0:
            daxes += 1
        j = np.atleast_1d((0,)*len(daxes) if j == () else j)
        assert len(j) == len(daxes)
        sa = np.array(sl)
        if len(daxes) > 0:
            sa.T[daxes] = j if isinstance(j, int) else np.array(j)[:, None]
        return np.atleast_2d(sa)

    def _get_ndiag_slices_and_dims(self, j=()):
        """Return nondiagonal slices and their accumulated dimensions"""
        sl = self._get_ndiag_slices(j)
        dims = self._get_ndiag_cum_dofs()
        return sl, dims


class CompositeSpace:
    """Class for composite tensorproductspaces.

    The mixed spaces are Cartesian products of :class:`.TensorProductSpace` or
    other instances of :class:`.CompositeSpace`.

    Parameters
    ----------
    spaces : list
        List of spaces
    """

    def __init__(self, spaces):
        self.spaces = spaces
        self.forward = VectorTransform([space.forward for space in spaces])
        self.backward = VectorTransform([space.backward for space in spaces])
        self.scalar_product = VectorTransform([space.scalar_product for space in spaces])

    @property
    def is_composite_space(self):
        return 1

    def eval(self, points, coefficients, output_array=None, method=1):
        """Evaluate Function at points, given expansion coefficients

        Parameters
        ----------
        points : float or array of floats
        coefficients : array
            Expansion coefficients
        output_array : array, optional
            Return array, function values at points
        method : int, optional
            Chooses implementation. Method 0 is a low-memory cython
            version. Using method = 1 (default) leads to a faster cython
            implementation that, on the downside, uses more memory.
            The final, method = 2, is a python implementation used mainly
            for verification.
        """
        if output_array is None:
            output_array = np.zeros((len(self.flatten()), points.shape[-1]), dtype=self.forward.input_array.dtype)
        for i, space in enumerate(self.flatten()):
            output_array.__array__()[i] = space.eval(points, coefficients.__array__()[i], output_array.__array__()[i], method)
        return output_array

    def convolve(self, a_hat, b_hat, ab_hat):
        """Convolution of a_hat and b_hat

        Parameters
        ----------
        a_hat : array
            Input array of shape and type as output array from
            self.forward, or instance of :class:`.Function`
        b_hat : array
            Input array of shape and type as output array from
            self.forward, or instance of :class:`.Function`
        ab_hat : array
            Return array of same type and shape as a_hat and b_hat

        Note
        ----
        Note that self should have bases with padding for this method to give
        a convolution without aliasing. The padding is specified when creating
        instances of bases for the TensorProductSpace.

        FIXME Efficiency due to allocation
        """
        N = list(self.backward.output_array.shape)
        a = np.zeros([self.dimensions]+N, dtype=self.backward.output_array.dtype)
        b = np.zeros([self.dimensions]+N, dtype=self.backward.output_array.dtype)
        a = self.backward(a_hat, a)
        b = self.backward(b_hat, b)
        ab_hat = self.forward(a*b, ab_hat)
        return ab_hat

    @property
    def dimensions(self):
        return self.flatten()[0].dimensions

    @property
    def rank(self):
        """Return rank of space

        Note
        ----
        This is 1 for all composite spaces, since we use a flat
        storage of all composite arrays.
        """
        return 1

    @property
    def tensor_rank(self):
        """Return rank of tensor

        Note
        ----
        This is the number of free indices in the tensor, 0 for scalar,
        1 for vector etc. It is None for a composite space that is not a
        tensor.
        """
        return None

    def dim(self):
        """Return dimension of ``self`` (degrees of freedom)"""
        return sum(self.dims())

    def dims(self):
        """Return dimensions (degrees of freedom) of all spaces in ``self``"""
        s = []
        for space in self.flatten():
            s.append(space.dim())
        return s

    def dims_composite(self):
        """Return dimensions per axis (degrees of freedom) of all spaces in ``self``"""
        s = []
        for space in self.flatten():
            s.append(space.dims())
        return np.array(s)

    def get_offsets(self):
        """Return offsets for spaces"""
        return np.concatenate((np.zeros((1, self.dimensions), dtype=int),
                               np.cumsum(self.dims_composite(), axis=0)))

    def _get_ndiag_cum_dofs(self):
        """Return the cumulative sum of degrees of freedom along nondiagonal axes"""
        dc = self.dims_composite()
        daxes = self.get_diagonal_axes()
        dc.T[daxes] = 1
        dci = np.prod(dc, axis=1)
        dims = np.array([0]+np.cumsum(dci).tolist())
        return dims

    def _get_ndiag_slices(self, j=()):
        sl = self.slice()
        daxes = self.get_diagonal_axes()
        if len(daxes) > 0:
            daxes += 1
        j = np.atleast_1d((0,)*len(daxes) if j == () else j)
        assert len(j) == len(daxes)
        sa = np.array(sl)
        if len(daxes) > 0:
            sa.T[daxes] = np.array(j)[:, None]
        return sa

    def _get_ndiag_slices_and_dims(self, j=()):
        sl = self._get_ndiag_slices(j)
        dims = [0]
        for s in sl:
            d = np.prod([1 if isinstance(i, int) else i.stop-i.start for i in s])
            dims.append(dims[-1]+d)
        return sl, np.array(dims)

    def slice(self):
        """The slices of dofs for each dimension"""
        return tuple(tuple([i]+list(space.slice())) for i, space in enumerate(self.flatten()))

    def size(self, forward_output=False):
        """Return number of elements in :class:`.CompositeSpace`"""
        N = self.shape(forward_output)
        if forward_output:
            return np.sum([np.prod(s) for s in N])
        else:
            return np.prod(N)

    def shape(self, forward_output=False):
        """Return shape of arrays for :class:`.CompositeSpace`

        Parameters
        ----------
        forward_output : bool, optional
            If True then return shape of an array that is the result of a
            forward transform. If False then return shape of physical
            space, i.e., the input to a forward transform.

        Note
        ----
        A :class:`.CompositeSpace` may contain tensor product spaces
        of different shape in spectral space. Hence this function returns a
        list of shapes and not one single tuple.
        """
        if forward_output:
            s = []
            for space in self.flatten():
                s.append(space.shape(forward_output))
        else:
            s = self.flatten()[0].shape(forward_output)
            s = (self.num_components(),) + s
        return s

    def global_shape(self, forward_output=False):
        """Return global shape for :class:`.CompositeSpace`

        Parameters
        ----------
        forward_output : bool, optional
            If True then return shape of an array that is the result of a
            forward transform. If False then return shape of physical
            space, i.e., the input to a forward transform.

        """
        s = self.flatten()[0].global_shape(forward_output)
        return (self.num_components(),) + s

    def local_slice(self, forward_output=False):
        """The local view into the global data

        Parameters
        ----------
        forward_output : bool, optional
            Return local slices of output array (spectral space) if True, else
            return local slices of input array (physical space)

        """
        s = self.flatten()[0].local_slice(forward_output)
        return (slice(None),) + s

    def num_components(self):
        f = self.flatten()
        return len(f)

    def flatten(self):
        s = []
        def _recursiveflatten(l, s):
            if hasattr(l, 'spaces'):
                for i in l.spaces:
                    _recursiveflatten(i, s)
            else:
                s.append(l)
        _recursiveflatten(self, s)
        return s

    def compatible_base(self, space):
        assert self.rank == space.rank
        compatible = True
        for space0, space1 in zip(self.flatten(), space.flatten()):
            if space0.compatible_base(space1) is False:
                compatible = False
                break
        return compatible

    def get_refined(self, N):
        raise NotImplementedError

    def get_orthogonal(self):
        raise NotImplementedError

    def get_dealiased(self, padding_factor=1.5, dealias_direct=False):
        raise NotImplementedError

    def __getitem__(self, i):
        return self.spaces[i]

    def __getattr__(self, name):
        obj = object.__getattribute__(self, 'spaces')
        assert name not in ('bases',)
        return getattr(obj[0], name)

    def __len__(self):
        return len(self.spaces)


class VectorSpace(CompositeSpace):
    """Vector of :class:`.TensorProductSpace` instances.

    This is simply a special :class:`.CompositeSpace` where the number of
    instances of the :class:`.TensorProductSpace` class equals the geometrical
    dimension of the problem.

    For example, a TensorProductSpace created by a tensorproduct of 2 1D
    spaces, will have vectors of length 2. A TensorProductSpace created from 3
    1D spaces will have vectors of length 3.

    Parameters
    ----------
    space : :class:`.TensorProductSpace` or list of ndim instances of the
    :class:`.TensorProductSpace` class to create vector from.

    """

    def __init__(self, space):
        if isinstance(space, list):
            assert len(space) == space[0].dimensions
            spaces = space
        else:
            spaces = [space]*space.dimensions
        CompositeSpace.__init__(self, spaces)

    def num_components(self):
        """Return number of spaces in mixed space"""
        assert len(self.spaces) == self.dimensions
        return self.dimensions

    @property
    def tensor_rank(self):
        return 1

    def shape(self, forward_output=False):
        """Return shape of arrays for :class:`.VectorSpace`

        Parameters
        ----------
        forward_output : bool, optional
            If True then return shape of an array that is the result of a
            forward transform. If False then return shape of physical
            space, i.e., the input to a forward transform.

        """
        s = self.flatten()[0].shape(forward_output)
        s = (self.num_components(),) + s
        return s

    def get_refined(self, N):
        if np.all([s == self.spaces[0] for s in self.spaces[1:]]):
            return VectorSpace(self.spaces[0].get_refined(N))
        return VectorSpace([s.get_refined(N) for s in self.spaces])

    def get_dealiased(self, padding_factor=1.5, dealias_direct=False):
        if padding_factor == 1 and dealias_direct == False:
            return self
        if np.all([s == self.spaces[0] for s in self.spaces[1:]]):
            return VectorSpace(self.spaces[0].get_dealiased(padding_factor, dealias_direct))
        return VectorSpace([s.get_dealiased(padding_factor, dealias_direct) for s in self.spaces])

    def get_orthogonal(self):
        if np.all([s == self.spaces[0] for s in self.spaces[1:]]):
            return VectorSpace(self.spaces[0].get_orthogonal())
        return VectorSpace([s.get_orthogonal() for s in self.spaces])

class TensorSpace(VectorSpace):
    """A special :class:`.CompositeSpace` for second rank tensors.

    A TensorProductSpace created by a tensorproduct of 2 1D
    bases, will have tensors of length 4. A TensorProductSpace created from 3
    1D bases will have tensors of length 9.

    Parameters
    ----------
    space : :class:`.TensorProductSpace` or list of ndim :class:`.VectorSpace`s
        Spaces to create vector from

    """

    def __init__(self, space):
        if isinstance(space, list):
            assert isinstance(space[0], VectorSpace)
            assert len(space) == space[0].dimensions
            spaces = space
        elif isinstance(space, VectorSpace):
            spaces = [space]*space.dimensions
        else:
            assert isinstance(space, TensorProductSpace)
            spaces = [VectorSpace(space)]*space.dimensions
        CompositeSpace.__init__(self, spaces)

    def num_components(self):
        return self.dimensions**2

    @property
    def tensor_rank(self):
        return 2

    def get_refined(self, N):
        if np.all([s == self.spaces[0] for s in self.spaces[1:]]):
            return TensorSpace(self.spaces[0].get_refined(N))
        return TensorSpace([s.get_refined(N) for s in self.spaces])

    def get_dealiased(self, padding_factor=1.5, dealias_direct=False):
        if padding_factor == 1 and dealias_direct == False:
            return self
        if np.all([s == self.spaces[0] for s in self.spaces[1:]]):
            return TensorSpace(self.spaces[0].get_dealiased(padding_factor, dealias_direct))
        return TensorSpace([s.get_dealiased(padding_factor, dealias_direct) for s in self.spaces])

    def get_orthogonal(self):
        if np.all([s == self.spaces[0] for s in self.spaces[1:]]):
            return TensorSpace(self.spaces[0].get_orthogonal())
        return TensorSpace([s.get_orthogonal() for s in self.spaces])

class VectorTransform:

    __slots__ = ('_transforms',)

    def __init__(self, transforms):
        self._transforms = []
        for transform in transforms:
            if isinstance(transform, VectorTransform):
                self._transforms += transform._transforms
            else:
                self._transforms.append(transform)

    def __getattr__(self, name):
        obj = object.__getattribute__(self, '_transforms')
        if name == '_transforms':
            return obj
        return getattr(obj[0], name)

    def __call__(self, input_array, output_array, kind=None, **kw):
        mesh = kw.get('mesh', None) # only backward transform
        for i, transform in enumerate(self._transforms):
            if mesh is not None:
                mi = mesh[i] if isinstance(mesh, CompositeSpace) else mesh
                kw['mesh'] = mi
            output_array.__array__()[i] = transform(input_array.__array__()[i],
                                                    output_array.__array__()[i],
                                                    kind=kind, **kw)
        return output_array


class Convolve:
    r"""Class for convolving without truncation.

    The convolution of :math:`\hat{a}` and :math:`\hat{b}` is computed by first
    transforming backwards with padding::

        a = Tp.backward(a_hat)
        b = Tp.backward(b_hat)

    and then transforming the product ``a*b`` forward without truncation::

        ab_hat = T.forward(a*b)

    where Tp is a :class:`.TensorProductSpace` for regular padding, and
    T is a TensorProductSpace with no padding, but using the shape
    of the padded a and b arrays.

    For convolve with truncation forward, use just the convolve method
    of the Tp space instead.

    Parameters
    ----------
    padding_space : :class:`.TensorProductSpace`
        Space with regular padding backward and truncation forward.
    """

    def __init__(self, padding_space):
        self.padding_space = padding_space
        shape = padding_space.shape()
        bases = []
        for i, base in enumerate(padding_space.bases):
            newbase = base.__class__(shape[i], padding_factor=1.0)
            bases.append(newbase)
        axes = []
        for axis in padding_space.axes:
            axes.append(axis[0])
        newspace = TensorProductSpace(padding_space.comm, bases, axes=axes)
        self.newspace = newspace

    def __call__(self, a_hat, b_hat, ab_hat=None):
        """Compute convolution of a_hat and b_hat without truncation

        Parameters
        ----------
        a_hat : :class:`.Function`
        b_hat : :class:`.Function`
        ab_hat : :class:`.Function`
        """
        Tp = self.padding_space
        T = self.newspace
        if ab_hat is None:
            ab_hat = Function(T)

        a = Array(Tp)
        b = Array(Tp)
        a = Tp.backward(a_hat, a)
        b = Tp.backward(b_hat, b)
        ab_hat = T.forward(a*b, ab_hat)
        return ab_hat


class BoundaryValues:
    """Class for setting nonhomogeneous boundary conditions inside a multi-
    dimensional :class:`.TensorProductSpace`.

    Parameters
    ----------
    T : :class:`.TensorProductSpace`
    bc : :class:`.BoundaryConditions`
    """
    # pylint: disable=protected-access, redefined-outer-name, dangerous-default-value, unsubscriptable-object

    def __init__(self, T, bc):
        self.base = T
        self.tensorproductspace = None
        self.bc = bc
        self.bcs = bc.orderedvals()       # Processed bc
        self.bcs_final = bc.orderedvals() # Data. May differ from bcs only for TensorProductSpaces
        self.axis = 0
        self.bc_time = 0

    def update(self, time=None):
        from shenfun.forms.project import Project
        tt = sp.symbols('t', real=True)
        bcs = self.bc.orderedvals()
        update_tensor = False
        if self.has_nonhomogeneous_bcs:
            for i, bci in enumerate(bcs):
                if isinstance(bci, Project):
                    bci()
                    update_tensor = True
                elif isinstance(bci, sp.Expr) and time is not None:
                    if tt in bci.free_symbols:
                        self.bc_time = time
                        bcs[i] = bci.subs(tt, time)
                        update_tensor = True
        if update_tensor:
            self.bcs = bcs
            self.bcs_final[:] = self.bcs
            if self.tensorproductspace is not None:
                self.set_tensor_bcs(self.base, self.tensorproductspace)

    def set_tensor_bcs(self, this_base, T):
        """Set correct boundary values for tensor, using values in self.bc

        Parameters
        ----------
        this_base : instance of :class:`.SpectralBase`
        T : instance of :class:`.TensorProductSpace`

        Note
        ----
        To modify boundary conditions on the fly, modify first self.bc and then
        call this function. The boundary condition can then be applied as before.
        """
        from shenfun import project
        from shenfun.utilities import split

        self.axis = this_base.axis
        self.base = this_base
        self.tensorproductspace = T

        if isinstance(T, SpectralBase):
            pass
        elif this_base.has_nonhomogeneous_bcs:
            # Setting an inhomogeneous boundary condition in a TensorProductSpace
            # is more involved than for a single dimension, and the routine will
            # depend on the order of the bases. Basically, the inhomogeneous value
            # needs to be projected to a space consisting of all the other function
            # spaces, except the inhomogeneous one. This would be very easy if it was
            # not for MPI.
            axis = None
            number_of_bases_after_this = 0
            for axes in reversed(T.axes):
                base = T.bases[axes[-1]]
                if base == this_base:
                    axis = base.axis
                else:
                    if axis is None:
                        number_of_bases_after_this += 1

            if self.has_nonhomogeneous_bcs() is False:
                for i in range(len(self.bcs)):
                    self.bcs[i] = self.bcs_final[i] = 0
                return

            # Set boundary values
            # These are values set at the end of a transform in an
            # inhomogeneous space, but before any other transforms

            b = Array(T)
            s = T.local_slice(False)[self.axis]
            num_bcs = self.bc.num_bcs()

            for j, bci in enumerate(self.bc.orderedvals()):
                if isinstance(bci, sp.Expr):
                    X = T.local_mesh(True)
                    for sym in bci.free_symbols:
                        tt = sp.symbols('t', real=True)
                        if sym == tt:
                            bci = bci.subs(tt, self.bc_time)
                    sym0 = bci.free_symbols
                    lbci = sp.lambdify(tuple(sym0), bci, 'numpy')
                    Yi = []
                    for sym in sym0:
                        k = 'xyzrs'.index(str(sym))
                        Yi.append(X[k][this_base.si[j]])
                    f_bci = lbci(*Yi)
                    # Put the value in the position of the bc dofs
                    if s.stop == int(this_base.N*this_base.padding_factor):
                        b[this_base.si[-num_bcs+j]] = f_bci

                elif isinstance(bci, Function):
                    # if bci is some Function that needs to be evaluated at the boundary
                    sl = list(T.local_slice(False))
                    sl.pop(self.axis)
                    sl = tuple(sl)
                    gs = list(T.global_shape())
                    gs.pop(self.axis)
                    gs = tuple(gs)
                    mesh = []
                    sj = this_base.si[0]
                    for axis, mj in enumerate(T.mesh(True)):
                        mj = np.broadcast_to(mj[sj], gs)
                        if axis != self.axis:
                            mesh.append(mj.flatten())
                    name = self.bc.orderednames()[j]
                    dm = this_base.domain[0] if 'L' in name else this_base.domain[1]
                    mesh.insert(self.axis, np.ones(len(mesh[0]))*dm)
                    xj = np.vstack(mesh)
                    newb = bci.function_space().eval(xj, bci).reshape(gs)
                    if s.stop == int(this_base.N*this_base.padding_factor):
                        b[this_base.si[-num_bcs+j]] = newb[sl]

                elif isinstance(bci, (Number, np.ndarray)):
                    if s.stop == int(this_base.N*this_base.padding_factor):
                        b[this_base.si[-num_bcs+j]] = bci

                else:
                    raise NotImplementedError

            if len(T.get_nonhomogeneous_axes()) == 1:
                for j, bci in enumerate(self.bc.orderedvals()):
                    from .spectralbase import FuncWrap
                    if number_of_bases_after_this == 0:
                        # Inhomogeneous base is the first to be transformed
                        b_hat = b
                    else:
                        i = number_of_bases_after_this
                        u_hat = T.forward._xfftn[i].input_array
                        dx = T.coors.get_sqrt_det_g()
                        msdict = split(dx)
                        c = msdict[0]['coeff']
                        fun = FuncWrap(lambda x, y, **kw: x, u_hat, u_hat) # Do-nothing
                        fwd = Transform(list(T.forward._xfftn[:i])+[fun],
                                        T.forward._transfer[:i], [[], []])
                        b_hat = fwd(c*b).copy()

                    # Now b_hat contains the correct slices in slm1 and slm2
                    # These are the values to use on intermediate steps.
                    # If for example a Dirichlet space is squeezed between two Fourier spaces
                    self.bcs[j] = b_hat[this_base.si[-num_bcs+j]].copy()
                b_hat = T.forward(b).copy()

                for j in range(self.bc.num_bcs()):
                    self.bcs_final[j] = b_hat[this_base.si[-num_bcs+j]].copy()

            elif len(T.get_nonhomogeneous_axes()) == 2:
                assert len(T.bases) == 2, 'Only implemented for 2D'
                bases = []
                for axis, base in enumerate(T.bases):
                    if not base is this_base:
                        bases.append(base.get_unplanned())

                other_base = bases[0]
                ua = Array(other_base)
                lr = {'left': 0, 'right': 1}
                num_bcs = this_base.bcs.num_bcs()
                df = 2./(other_base.domain[1]-other_base.domain[0])
                for i, bcj in enumerate(this_base.bcs.orderedvals()):
                    sym = sp.sympify(bcj).free_symbols
                    xx = sym.pop() if len(sym) == 1 else None
                    for lr_other, bco in other_base.bcs.items():
                        xj = other_base.domain[lr[lr_other]]
                        for key in bco.keys():
                            if xx is not None:
                                if key == 'D':
                                    bco[key] = bcj.subs(xx, xj)
                                elif key == 'N':
                                    bco[key] = bcj.diff(xx, 1).subs(xx, xj)/df
                                elif key == 'N2':
                                    bco[key] = bcj.diff(xx, 2).subs(xx, xj)/df**2
                                elif key == 'N3':
                                    bco[key] = bcj.diff(xx, 3).subs(xx, xj)/df**3
                                elif key == 'N4':
                                    bco[key] = bcj.diff(xx, 4).subs(xx, xj)/df**4
                            else:
                                bco[key] = bcj
                        other_base.bc.bcs[:] = other_base.bcs.orderedvals()
                        other_base.bc.bcs_final[:] = other_base.bcs.orderedvals()
                    ua[:] = b[this_base.si[-num_bcs+i]]
                    self.bcs_final[i] = project(ua, other_base)

    def _add_to_orthogonal(self, u, uh):
        """Add contribution from boundary functions to ``u``

        Parameters
        ----------
        u : Function
            Apply boundary values to this Function
        uh : Function
            Containing correct boundary values of Function
        """
        B = self.base.get_bc_space()
        M = (B.stencil_matrix().T).astype(float)
        sl = B.slice()
        ds = uh.shape[self.base.axis] - B.N
        if ds > 0: # uh is padded, but B is not. Boundary dofs are in padded locations.
            sl = slice(sl.start + ds, sl.stop + ds)
        if u.ndim > 1:
            sl = self.base.sl[sl]
        for i, row in enumerate(M):
            u[self.base.si[i]] += np.sum(self.base.broadcast_to_ndims(row)*uh[sl], axis=self.base.axis)

    def _add_mass_rhs(self, u):
        """Add contribution of boundary functions to rhs before solving in forward transforms

        Parameters
        ----------
        u : Function
            Add boundary values to this Function

        """
        self.set_boundary_dofs(u)
        if not self.has_nonhomogeneous_bcs(): # there's no contribution for homogeneous bcs
            return
        coors = self.tensorproductspace.coors if self.tensorproductspace else self.base.coors
        M = self.base.get_bcmass_matrix(coors.get_sqrt_det_g())
        w0 = np.zeros_like(u)
        u -= M.matvec(u, w0, axis=self.base.axis)

    def set_boundary_dofs(self, u, final=False):
        """Apply boundary condition after solving, fixing boundary dofs

        Parameters
        ----------
        u : Function
            Apply boundary values to this Function
        final : bool
            Whether the function is fully transformed or not. Not is used in
            forward transforms, where the transform of this base may be in
            between the transforms of other bases. If final is True, then u
            must be a fully transformed Function.

        """
        M = self.bc.num_bcs()
        if final is True:
            for i in range(M):
                u[self.base.si[-(M)+i]] = self.bcs_final[i]
        else:
            for i in range(M):
                u[self.base.si[-(M)+i]] = self.bcs[i]

    def has_nonhomogeneous_bcs(self):
        for bc in self.bc.orderedvals():
            if isinstance(bc, Number):
                if not bc == 0:
                    return True
            elif isinstance(bc, np.ndarray):
                if not np.all(bc == 0):
                    return True
            elif isinstance(bc, sp.Expr):
                return True
        return False
