import numpy
from numpy import linalg

import warnings

import cupy
from cupy_backends.cuda.libs import cublas
from cupy.cuda import device
from cupy.linalg import _util

_batched_gesv_limit = 256


def get_batched_gesv_limit():
    global _batched_gesv_limit
    return _batched_gesv_limit


def set_batched_gesv_limit(limit):
    global _batched_gesv_limit
    _batched_gesv_limit = limit


def batched_gesv(a, b):
    """Solves multiple linear matrix equations using cublas<t>getr[fs]Batched().

    Computes the solution to system of linear equation ``ax = b``.

    Args:
        a (cupy.ndarray): The matrix with dimension ``(..., M, M)``.
        b (cupy.ndarray): The matrix with dimension ``(..., M)`` or
            ``(..., M, K)``.

    Returns:
        cupy.ndarray:
            The matrix with dimension ``(..., M)`` or ``(..., M, K)``.
    """
    _util._assert_cupy_array(a, b)
    _util._assert_nd_squareness(a)

    if not ((a.ndim == b.ndim or a.ndim == b.ndim + 1) and
            a.shape[:-1] == b.shape[:a.ndim - 1]):
        raise ValueError(
            'a must have (..., M, M) shape and b must have (..., M) '
            'or (..., M, K)')

    dtype = numpy.promote_types(a.dtype.char, 'f')
    if dtype == 'f':
        t = 's'
    elif dtype == 'd':
        t = 'd'
    elif dtype == 'F':
        t = 'c'
    elif dtype == 'D':
        t = 'z'
    else:
        raise TypeError('invalid dtype')
    getrf = getattr(cublas, t + 'getrfBatched')
    getrs = getattr(cublas, t + 'getrsBatched')

    bs = numpy.prod(a.shape[:-2]) if a.ndim > 2 else 1
    n = a.shape[-1]
    nrhs = b.shape[-1] if a.ndim == b.ndim else 1
    b_shape = b.shape
    a_data_ptr = a.data.ptr
    b_data_ptr = b.data.ptr
    a = cupy.ascontiguousarray(a.reshape(bs, n, n).transpose(0, 2, 1),
                               dtype=dtype)
    b = cupy.ascontiguousarray(b.reshape(bs, n, nrhs).transpose(0, 2, 1),
                               dtype=dtype)
    if a.data.ptr == a_data_ptr:
        a = a.copy()
    if b.data.ptr == b_data_ptr:
        b = b.copy()

    if n > get_batched_gesv_limit():
        warnings.warn('The matrix size ({}) exceeds the set limit ({})'.
                      format(n, get_batched_gesv_limit()))

    handle = device.get_cublas_handle()
    lda = n
    a_step = lda * n * a.itemsize
    a_array = cupy.arange(a.data.ptr, a.data.ptr + a_step * bs, a_step,
                          dtype=cupy.uintp)
    ldb = n
    b_step = ldb * nrhs * b.itemsize
    b_array = cupy.arange(b.data.ptr, b.data.ptr + b_step * bs, b_step,
                          dtype=cupy.uintp)
    pivot = cupy.empty((bs, n), dtype=numpy.int32)
    dinfo = cupy.empty((bs,), dtype=numpy.int32)
    info = numpy.empty((1,), dtype=numpy.int32)
    # LU factorization (A = L * U)
    getrf(handle, n, a_array.data.ptr, lda, pivot.data.ptr, dinfo.data.ptr, bs)
    _util._check_cublas_info_array_if_synchronization_allowed(getrf, dinfo)
    # Solves Ax = b
    getrs(handle, cublas.CUBLAS_OP_N, n, nrhs, a_array.data.ptr, lda,
          pivot.data.ptr, b_array.data.ptr, ldb, info.ctypes.data, bs)
    if info[0] != 0:
        msg = 'Error reported by {} in cuBLAS. '.format(getrs.__name__)
        if info[0] < 0:
            msg += 'The {}-th parameter had an illegal value.'.format(-info[0])
        raise linalg.LinAlgError(msg)

    return b.transpose(0, 2, 1).reshape(b_shape)


def iamax(x, out=None):
    """Finds the (smallest) index of the element with the maximum magnitude.

    Note: The result index is 1-based index (not 0-based index).
    """
    return _iamaxmin(x, out, 'amax')


def iamin(x, out=None):
    """Finds the (smallest) index of the element with the minimum magnitude.

    Note: The result index is 1-based index (not 0-based index).
    """
    return _iamaxmin(x, out, 'amin')


def _iamaxmin(x, out, name):
    if x.ndim != 1:
        raise ValueError('x must be a 1D array (actual: {})'.format(x.ndim))

    dtype = x.dtype.char
    if dtype == 'f':
        t = 's'
    elif dtype == 'd':
        t = 'd'
    elif dtype == 'F':
        t = 'c'
    elif dtype == 'D':
        t = 'z'
    else:
        raise TypeError('invalid dtype')
    func = getattr(cublas, 'i' + t + name)

    handle = device.get_cublas_handle()
    result_dtype = 'i'
    result_ptr, result, orig_mode = _setup_result_ptr(
        handle, out, result_dtype)
    func(handle, x.size, x.data.ptr, 1, result_ptr)
    cublas.setPointerMode(handle, orig_mode)

    if out is None:
        out = result
    elif out.dtype != result_dtype:
        out[...] = result
    return out


def asum(x, out=None):
    """Computes the sum of the absolute of x."""
    if x.ndim != 1:
        raise ValueError('x must be a 1D array (actual: {})'.format(x.ndim))

    dtype = x.dtype.char
    if dtype == 'f':
        func = cublas.sasum
    elif dtype == 'd':
        func = cublas.dasum
    elif dtype == 'F':
        func = cublas.scasum
    elif dtype == 'D':
        func = cublas.dzasum
    else:
        raise TypeError('invalid dtype')

    handle = device.get_cublas_handle()
    result_dtype = dtype.lower()
    result_ptr, result, orig_mode = _setup_result_ptr(
        handle, out, result_dtype)
    func(handle, x.size, x.data.ptr, 1, result_ptr)
    cublas.setPointerMode(handle, orig_mode)

    if out is None:
        out = result
    elif out.dtype != result_dtype:
        out[...] = result
    return out


def axpy(a, x, y):
    """Computes y += a * x.

    (*) y will be updated.
    """
    _check_two_vectors(x, y)

    dtype = x.dtype.char
    if dtype == 'f':
        func = cublas.saxpy
    elif dtype == 'd':
        func = cublas.daxpy
    elif dtype == 'F':
        func = cublas.caxpy
    elif dtype == 'D':
        func = cublas.zaxpy
    else:
        raise TypeError('invalid dtype')

    handle = device.get_cublas_handle()
    a_ptr, orig_mode = _setup_scalar_ptr(handle, a, dtype)
    func(handle, x.size, a_ptr, x.data.ptr, 1, y.data.ptr, 1)
    cublas.setPointerMode(handle, orig_mode)


def dot(x, y, out=None):
    """Computes the dot product of x and y."""
    dtype = x.dtype.char
    if dtype == 'f':
        func = cublas.sdot
    elif dtype == 'd':
        func = cublas.ddot
    elif dtype in 'FD':
        raise TypeError('Use dotu() or dotc() for complex dtype')
    else:
        raise TypeError('invalid dtype')
    _check_two_vectors(x, y)

    handle = device.get_cublas_handle()
    result_dtype = dtype
    result_ptr, result, orig_mode = _setup_result_ptr(
        handle, out, result_dtype)
    func(handle, x.size, x.data.ptr, 1, y.data.ptr, 1, result_ptr)
    cublas.setPointerMode(handle, orig_mode)

    if out is None:
        out = result
    elif out.dtype != result_dtype:
        out[...] = result
    return out


def dotu(x, y, out=None):
    """Computes the dot product of x and y."""
    dtype = x.dtype.char
    if dtype in 'fd':
        return dot(x, y, out=out)
    elif dtype == 'F':
        func = cublas.cdotu
    elif dtype == 'D':
        func = cublas.zdotu
    else:
        raise TypeError('invalid dtype')
    _check_two_vectors(x, y)

    handle = device.get_cublas_handle()
    result_dtype = dtype
    result_ptr, result, orig_mode = _setup_result_ptr(
        handle, out, result_dtype)
    func(handle, x.size, x.data.ptr, 1, y.data.ptr, 1, result_ptr)
    cublas.setPointerMode(handle, orig_mode)

    if out is None:
        out = result
    elif out.dtype != result_dtype:
        out[...] = result
    return out


def dotc(x, y, out=None):
    """Computes the dot product of x.conj() and y."""
    dtype = x.dtype.char
    if dtype in 'fd':
        return dot(x, y, out=out)
    elif dtype == 'F':
        func = cublas.cdotc
    elif dtype == 'D':
        func = cublas.zdotc
    else:
        raise TypeError('invalid dtype')
    _check_two_vectors(x, y)

    handle = device.get_cublas_handle()
    result_dtype = dtype
    result_ptr, result, orig_mode = _setup_result_ptr(
        handle, out, result_dtype)
    func(handle, x.size, x.data.ptr, 1, y.data.ptr, 1, result_ptr)
    cublas.setPointerMode(handle, orig_mode)

    if out is None:
        out = result
    elif out.dtype != result_dtype:
        out[...] = result
    return out


def nrm2(x, out=None):
    """Computes the Euclidean norm of vector x."""
    if x.ndim != 1:
        raise ValueError('x must be a 1D array (actual: {})'.format(x.ndim))

    dtype = x.dtype.char
    if dtype == 'f':
        func = cublas.snrm2
    elif dtype == 'd':
        func = cublas.dnrm2
    elif dtype == 'F':
        func = cublas.scnrm2
    elif dtype == 'D':
        func = cublas.dznrm2
    else:
        raise TypeError('invalid dtype')

    handle = device.get_cublas_handle()
    result_dtype = dtype.lower()
    result_ptr, result, orig_mode = _setup_result_ptr(
        handle, out, result_dtype)
    func(handle, x.size, x.data.ptr, 1, result_ptr)
    cublas.setPointerMode(handle, orig_mode)

    if out is None:
        out = result
    elif out.dtype != result_dtype:
        out[...] = result
    return out


def scal(a, x):
    """Computes x *= a.

    (*) x will be updated.
    """
    if x.ndim != 1:
        raise ValueError('x must be a 1D array (actual: {})'.format(x.ndim))

    dtype = x.dtype.char
    if dtype == 'f':
        func = cublas.sscal
    elif dtype == 'd':
        func = cublas.dscal
    elif dtype == 'F':
        func = cublas.cscal
    elif dtype == 'D':
        func = cublas.zscal
    else:
        raise TypeError('invalid dtype')

    handle = device.get_cublas_handle()
    a_ptr, orig_mode = _setup_scalar_ptr(handle, a, dtype)
    func(handle, x.size, a_ptr, x.data.ptr, 1)
    cublas.setPointerMode(handle, orig_mode)


def _check_two_vectors(x, y):
    if x.ndim != 1:
        raise ValueError('x must be a 1D array (actual: {})'.format(x.ndim))
    if y.ndim != 1:
        raise ValueError('y must be a 1D array (actual: {})'.format(y.ndim))
    if x.size != y.size:
        raise ValueError('x and y must be the same size (actual: {} and {})'
                         ''.format(x.size, y.size))
    if x.dtype != y.dtype:
        raise TypeError('x and y must be the same dtype (actual: {} and {})'
                        ''.format(x.dtype, y.dtype))


def _setup_result_ptr(handle, out, dtype):
    mode = cublas.getPointerMode(handle)
    if out is None or isinstance(out, cupy.ndarray):
        if out is None or out.dtype != dtype:
            result = cupy.empty([], dtype=dtype)
        else:
            result = out
        result_ptr = result.data.ptr
        cublas.setPointerMode(handle, cublas.CUBLAS_POINTER_MODE_DEVICE)
    elif isinstance(out, numpy.ndarray):
        if out.dtype != dtype:
            result = numpy.empty([], dtype=dtype)
        else:
            result = out
        result_ptr = result.ctypes.data
        cublas.setPointerMode(handle, cublas.CUBLAS_POINTER_MODE_HOST)
    else:
        raise TypeError('out must be either cupy or numpy ndarray')
    return result_ptr, result, mode


def _setup_scalar_ptr(handle, a, dtype):
    mode = cublas.getPointerMode(handle)
    if isinstance(a, cupy.ndarray):
        if a.dtype != dtype:
            a = cupy.array(a, dtype=dtype)
        a_ptr = a.data.ptr
        cublas.setPointerMode(handle, cublas.CUBLAS_POINTER_MODE_DEVICE)
    else:
        if not (isinstance(a, numpy.ndarray) and a.dtype == dtype):
            a = numpy.array(a, dtype=dtype)
        a_ptr = a.ctypes.data
        cublas.setPointerMode(handle, cublas.CUBLAS_POINTER_MODE_HOST)
    return a_ptr, mode
