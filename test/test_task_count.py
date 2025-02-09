import dask.array
import numpy as np
import zarr

from ngff_zarr import NgffImage, to_ngff_image, to_multiscales, to_ngff_zarr, from_ngff_zarr, task_count

def test_memory_usage():
    arr = np.random.randint(0, 255, size=(4,4,4), dtype=np.uint8)
    arr = dask.array.from_array(arr, chunks=2)
    image = to_ngff_image(arr)
    multiscales = to_multiscales(image, scale_factors=[], chunks=2)
    store = zarr.storage.MemoryStore()
    to_ngff_zarr(store, multiscales)
    multiscales = from_ngff_zarr(store)

    image = multiscales.images[0]
    arr = image.data
    assert len(arr.dask) == 9
    count = task_count(image)
    assert count == 9
    count = task_count(image, {'z'})
    assert count == 13

    arr1 = arr + 1
    assert len(arr1.dask) == 17
    image.data = arr1
    count = task_count(image)
    assert count == 17
    count = task_count(image, {'z'})
    assert count == 21
