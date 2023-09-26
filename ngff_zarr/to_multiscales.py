from typing import Union, Optional, Sequence, Mapping, Dict, Tuple, Any, List
from typing_extensions import Literal
from collections.abc import MutableMapping
import time
import shutil
from pathlib import Path
import atexit
import signal

from zarr.core import Array as ZarrArray
from numpy.typing import ArrayLike
from dask.array.core import Array as DaskArray
import numpy as np
import zarr
import dask

from .methods._dask_image import _downsample_dask_image
from .methods._itk import _downsample_itk_bin_shrink, _downsample_itk_gaussian, _downsample_itk_label
from .to_ngff_image import to_ngff_image
from .ngff_image import NgffImage
from .multiscales import Multiscales
from .zarr_metadata import Metadata, Axis, Translation, Scale, Dataset
from .methods import Methods
from .config import config
from .rich_dask_progress import NgffProgress, NgffProgressCallback
from .memory_usage import memory_usage

from .methods._support import _spatial_dims

def _ngff_image_scale_factors(ngff_image, min_length, out_chunks):
    sizes = { d: s for d, s in zip(ngff_image.dims, ngff_image.data.shape) if d in _spatial_dims }
    scale_factors = []
    dims = ngff_image.dims
    previous = { d: 1 for d in _spatial_dims.intersection(dims) }
    sizes_array = np.array(list(sizes.values()))
    double_chunks = np.array([2*out_chunks[d] for d in _spatial_dims.intersection(out_chunks)])
    while (sizes_array > double_chunks).all():
        max_size = np.array(list(sizes.values())).max()
        to_skip = { d: sizes[d] <= max_size / 2 for d in previous.keys() }
        scale_factor = {}
        for dim in previous.keys():
            if to_skip[dim] or sizes[dim] / 2 < out_chunks[dim]:
                scale_factor[dim] = previous[dim]
                continue
            scale_factor[dim] = 2 * previous[dim]

            sizes[dim] = int(sizes[dim] / 2)
        sizes_array = np.array(list(sizes.values()))
        previous = scale_factor
        # There should be sufficient data in the result for statistics, etc.
        if (np.prod(sizes_array) / min_length) < 2:
            break
        scale_factors.append(scale_factor)

    return scale_factors

def _large_image_serialization(image: NgffImage, progress: Optional[Union[NgffProgress, NgffProgressCallback]]):
    if "z" in image.dims:
        optimized_chunks = 512
    else:
        optimized_chunks = 1024
    base_path = f"{image.name}-cache-{time.time()}"

    cache_store = config.cache_store
    base_path_removed = False
    def remove_from_cache_store(sig_id, frame):
        nonlocal base_path_removed
        if not base_path_removed:
            if isinstance(cache_store, zarr.storage.DirectoryStore):
                full_path = Path(cache_store.dir_path()) / base_path
                if full_path.exists():
                    shutil.rmtree(full_path, ignore_errors=True)
            else:
                zarr.storage.rmdir(cache_store, base_path)
            base_path_removed = True
    atexit.register(remove_from_cache_store, None, None)
    signal.signal(signal.SIGTERM, remove_from_cache_store)
    signal.signal(signal.SIGINT, remove_from_cache_store)

    data = image.data

    dims = list(image.dims)
    x_index = dims.index('x')
    y_index = dims.index('y')

    rechunks = {}
    for index, dim in enumerate(dims):
        if dim == 't':
            rechunks[index] = 1
        elif dim == 'c':
            rechunks[index] = 1
        else:
            rechunks[index] = min(optimized_chunks, data.shape[index])

    if 'z' in dims:
        z_index = dims.index('z')
        slice_bytes = data.dtype.itemsize * data.shape[x_index] * data.shape[y_index]

        slab_slices = min(int(np.ceil(config.memory_target / slice_bytes)), data.shape[z_index])
        if optimized_chunks < data.shape[z_index]:
            slab_slices = min(slab_slices, optimized_chunks)
        rechunks[z_index] = slab_slices

        path = f"{base_path}/slabs"
        slabs = data.rechunk(rechunks)

        chunks = tuple([c[0] for c in slabs.chunks])
        optimized = dask.array.Array(dask.array.optimize(slabs.__dask_graph__(),
            slabs.__dask_keys__()), slabs.name,
            slabs.chunks, meta=slabs)
        zarr_array = zarr.creation.open_array(
            shape=data.shape,
            chunks=chunks,
            dtype=data.dtype,
            store=cache_store,
            path=path,
            mode='a',
        )

        n_slabs = int(np.ceil(data.shape[z_index] / slab_slices))
        if progress:
            progress.add_cache_task(f"[blue]Caching z-slabs", n_slabs)
        for slab_index in range(n_slabs):
            if progress:
                if isinstance(progress, NgffProgressCallback):
                    progress.add_callback_task(f"[blue]Caching z-slabs {slab_index+1} of {n_slabs}")
                progress.update_cache_task_completed((slab_index+1))
            region = [slice(data.shape[i]) for i in range(data.ndim)]
            region[z_index] = slice(slab_index*slab_slices, min((slab_index+1)*slab_slices, data.shape[z_index]))
            region = tuple(region)
            arr_region = optimized[region]
            dask.array.to_zarr(
                arr_region,
                zarr_array,
                region=region,
                component=path,
                overwrite=False,
                compute=True,
                return_stored=False,
            )
        data = dask.array.from_zarr(cache_store, component=path)
        if optimized_chunks < data.shape[z_index] and slab_slices < optimized_chunks:
            rechunks[z_index] = optimized_chunks
            data = data.rechunk(rechunks)
            path = f"{base_path}/optimized_chunks"
            chunks = tuple([c[0] for c in optimized.chunks])
            data = data.rechunk(chunks)
            zarr_array = zarr.creation.open_array(
                shape=data.shape,
                chunks=chunks,
                dtype=data.dtype,
                store=cache_store,
                path=path,
                mode='a',
            )
            n_slabs = int(np.ceil(data.shape[z_index] / optimized_chunks))
            for slab_index in range(n_slabs):
                if progress:
                    if isinstance(progress, NgffProgressCallback):
                        progress.add_callback_task(f"[blue]Caching z-rechunk {slab_index+1} of {n_slabs}")
                    progress.update_cache_task_completed((slab_index+1))
                region = [slice(data.shape[i]) for i in range(data.ndim)]
                region[z_index] = slice(slab_index*optimized_chunks, min((slab_index+1)*optimized_chunks, data.shape[z_index]))
                region = tuple(region)
                arr_region = data[region]
                dask.array.to_zarr(
                    arr_region,
                    zarr_array,
                    region=region,
                    component=path,
                    overwrite=False,
                    compute=True,
                    return_stored=False,
                )
            data = dask.array.from_zarr(cache_store, component=path)
        else:
            data = data.rechunk(rechunks)
    else:
        data = data.rechunk(rechunks)
        # TODO: Slab, chunk optimized very large 2D images
        path = base_path + f"/optimized_chunks"
        if progress:
            progress.add_callback_task(f"[blue]Caching optimized chunks")
        dask.array.to_zarr(
            data,
            cache_store,
            component=path,
            overwrite=False,
            compute=True,
            return_stored=False,
        )
        data = dask.array.from_zarr(cache_store, component=path)

    image.data = data
    return image

def to_multiscales(
    data: Union[NgffImage, ArrayLike, MutableMapping, str, ZarrArray],
    layout: Literal['itk', 'cellmap'] = 'itk',
    scale_factors: Union[int, Sequence[Union[Dict[str, int], int]]] = 128,
    method: Optional[Methods] = None,
    chunks: Optional[
        Union[
            int,
            Tuple[int, ...],
            Tuple[Tuple[int, ...], ...],
            Mapping[Any, Union[None, int, Tuple[int, ...]]],
        ]
    ] = None,
    progress: Optional[Union[NgffProgress, NgffProgressCallback]] = None,
    cache: Optional[bool] = None,
) -> Multiscales:
    """
    Generate multiple resolution scales for the OME-NGFF standard data model.

    data: NgffImage, ArrayLike, ZarrArray, MutableMapping, str
        Multi-dimensional array that provides the image pixel values, or image pixel values + image metadata when an NgffImage.

    layout: str, must be either 'cellmap' or 'itk'. Default is 'itk'.
        Determines the on-disk layout for the multiscale data. 
        
        If 'itk' is chosen, each scale level will be stored in a Zarr array in a 
        subgroup of the multiscale group. 
        
        For example, if the multiscale group is called 'foo', then choosing the 'itk'
        layout will result arrays with paths like 
        `foo/scale0/image` 
        `foo/scale1/image`

        Additionally, an extra piece of metadata will be added to each of the `scaleX` 
        groups that facilitates compatibility with the python library Xarray.

        If 'ngff' is chosen, then storage will use the layout given as an example in the
        OME-NGFF specification, with one modification: instead of saving arrays with 
        names like 0,1, etc, arrays will be saved with names that are prefixed by 's'. 

        For example, if the multiscale group is called 'foo', then choosing the 'ngff'
        layout will result arrays with paths like 
        `foo/s0` 
        `foo/s1`  

    scale_factors : int of minimum length, int per scale or dict of spatial dimension int's per scale
        If a single integer, scale factors in spatial dimensions will be increased by a factor of two until this minimum length is reached.
        If a list, integer scale factors to apply uniformly across all spatial dimensions or
        along individual spatial dimensions.
        Examples: 64 or [2, 4] or [{'x': 2, 'y': 4 }, {'x': 5, 'y': 10}]

    chunks : Dask array chunking specification, optional
        Specify the chunking used in each output scale.

    cache : bool, optional
        Cache intermediate results to disk to limit memory consumption. If None, the default, determine based on ngff_zarr.config.memory_target.

    progress:
        Optional progress logger

    Returns
    -------

    multiscales: Multiscales
        NgffImage for each resolution and NGFF multiscales metadata
    """
    image = data
    if isinstance(data, NgffImage):
        ngff_image = data
    else:
        ngff_image = to_ngff_image(data)

    # IPFS and visualization friendly default chunks
    if "z" in ngff_image.dims:
        default_chunks = 128
    else:
        default_chunks = 256
    default_chunks = {d: default_chunks for d in ngff_image.dims}
    if "t" in ngff_image.dims:
        default_chunks["t"] = 1
    out_chunks = chunks
    if out_chunks is None:
        out_chunks = default_chunks
    elif isinstance(out_chunks, int):
        out_chunks = {d: chunks for d in ngff_image.dims}
    elif isinstance(out_chunks, tuple):
        out_chunks = {d: chunks[i] for i, d in enumerate(ngff_image.dims)}

    da_out_chunks = tuple(out_chunks[d] for d in ngff_image.dims)
    if not isinstance(ngff_image.data, DaskArray):
        if isinstance(ngff_image.data, (ZarrArray, str, MutableMapping)):
            ngff_image.data = dask.array.from_zarr(ngff_image.data)
        else:
            ngff_image.data = dask.array.from_array(ngff_image.data)

    if isinstance(scale_factors, int):
        scale_factors = _ngff_image_scale_factors(ngff_image, scale_factors, out_chunks)

    # if cache is None and memory_usage(ngff_image) > config.memory_target or task_count(ngff_image) > config.task_target or cache:
    if cache is None and memory_usage(ngff_image) > config.memory_target or cache:
        ngff_image = _large_image_serialization(ngff_image, progress)

    ngff_image.data = ngff_image.data.rechunk(da_out_chunks)

    if method is None:
        method = Methods.DASK_IMAGE_GAUSSIAN

    if method is Methods.ITK_BIN_SHRINK:
        images = _downsample_itk_bin_shrink(
            ngff_image, default_chunks, out_chunks, scale_factors
        )
    elif method is Methods.ITK_GAUSSIAN:
        images = _downsample_itk_gaussian(
            ngff_image, default_chunks, out_chunks, scale_factors
        )
    elif method is Methods.DASK_IMAGE_GAUSSIAN:
        images = _downsample_dask_image(
            ngff_image, default_chunks, out_chunks, scale_factors, label=False
        )
    elif method is Methods.DASK_IMAGE_NEAREST:
        images = _downsample_dask_image(
            ngff_image, default_chunks, out_chunks, scale_factors, label="nearest"
        )
    elif method is Methods.DASK_IMAGE_MODE:
        images = _downsample_dask_image(
            ngff_image, default_chunks, out_chunks, scale_factors, label="mode"
        )

    axes = []
    for dim in ngff_image.dims:
        unit = None
        if ngff_image.axes_units and dim in ngff_image.axes_units:
            unit = ngff_image.axes_units[dim]
        if dim in {"x", "y", "z"}:
            axis = Axis(name=dim, type="space", unit=unit)
        elif dim == "c":
            axis = Axis(name=dim, type="channel", unit=unit)
        elif dim == "t":
            axis = Axis(name=dim, type="time", unit=unit)
        else:
            raise KeyError(f'Dimension identifier is not valid: {dim}')
        axes.append(axis)

    datasets = []
    for index, image in enumerate(images):
        if layout == 'itk':
            path = f"scale{index}/{ngff_image.name}"
        elif layout == 'cellmap':
            path = f"s{index}"
        else:
            msg = (f"Invalid layout specified: {layout}." 
                   "Layout must be either 'itk' or 'cellmap'")
            raise ValueError(msg)
        scale = []
        for dim in image.dims:
            if dim in image.scale:
                scale.append(image.scale[dim])
            else:
                scale.append(1.0)
        translation = []
        for dim in image.dims:
            if dim in image.translation:
                translation.append(image.translation[dim])
            else:
                translation.append(1.0)
        coordinateTransformations = [Scale(scale), Translation(translation)]
        dataset = Dataset(
            path=path, coordinateTransformations=coordinateTransformations
        )
        datasets.append(dataset)
    metadata = Metadata(axes=axes, datasets=datasets, name=ngff_image.name)

    multiscales = Multiscales(images, metadata, scale_factors, method, out_chunks)
    return multiscales
