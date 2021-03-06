

import logging as log

from builtins import range

from mpi4py import MPI

from .. import Engine
from ..base import Wrapper
from ..backends import get_backend

from .cpu import CpuDataset


class MpiMixin(object):

    def __init__(self, mpi_comm):
        self.mpi_comm = mpi_comm
        self.mpi_size = self.mpi_comm.Get_size()
        self.mpi_rank = self.mpi_comm.Get_rank()
        self.mpi_is_root = self.mpi_rank == 0

    def mpi_barrier(self):
        self.mpi_comm.Barrier()


class MpiCluster(Wrapper, MpiMixin):

    def __init__(self, name, comm=None, **kwargs):
        bname = kwargs.pop('backend', None)
        backend = get_backend(bname)

        bcomm = comm or __engine__.params['comm']
        self.cparams = backend, name, kwargs
        self.ceph_backend = (backend.name == 'ceph')
        instance = backend.Cluster(name, **kwargs)

        Wrapper.__init__(self, instance)
        MpiMixin.__init__(self, bcomm)

        if backend.name == 'memory':
            log.warning('MPI engine will work unexpectedly with RAM backend')

    def create_pool(self, *args, **kwargs):
        if self.mpi_is_root:
            self.instance.create_pool(*args, **kwargs)
        self.mpi_barrier()

        if self.ceph_backend:
            # Ceph processes require new connection to see the new pool
            self.instance.disconnect()
            backend, name, kwargs = self.cparams
            self.instance = backend.Cluster(name, **kwargs)
            self.instance.connect()
            self.mpi_barrier()

        return self.get_pool(*args, **kwargs)

    def get_pool(self, *args, **kwargs):
        pool = self.instance.get_pool(*args, **kwargs)
        return MpiPool(pool, self.mpi_comm)

    def del_pool(self, *args, **kwargs):
        self.mpi_barrier()
        if self.mpi_is_root:
            self.instance.del_pool(*args, **kwargs)
        self.mpi_barrier()

        if self.ceph_backend:
            # Ceph processes require new connection to clear the pool
            self.instance.disconnect()
            backend, name, kwargs = self.cparams
            self.instance = backend.Cluster(name, **kwargs)
            self.instance.connect()
            self.mpi_barrier()

    def __getitem__(self, pool_name):
        return self.get_pool(pool_name)


class MpiPool(Wrapper, MpiMixin):

    def __init__(self, pool, mpi_comm):
        Wrapper.__init__(self, pool)
        MpiMixin.__init__(self, mpi_comm)

    def create_dataset(self, name, *args, **kwargs):
        if self.mpi_is_root:
            ds = self.instance.create_dataset(name, *args, **kwargs)
        self.mpi_barrier()
        ds = self.get_dataset(name)
        if 'data' in kwargs:
            ds.load(kwargs['data'])
        return ds

    def get_dataset(self, *args, **kwargs):
        ds = self.instance.get_dataset(*args, **kwargs)
        return MpiDataset(ds, self.mpi_comm)

    def del_dataset(self, *args, **kwargs):
        self.mpi_barrier()
        if self.mpi_is_root:
            self.instance.del_dataset(*args, **kwargs)
        self.mpi_barrier()

    def __getitem__(self, ds_name):
        return self.get_dataset(ds_name)


class MpiDataset(CpuDataset, MpiMixin):

    def __init__(self, ds, mpi_comm):
        CpuDataset.__init__(self, ds)
        MpiMixin.__init__(self, mpi_comm)

    def create_chunk(self, idx, *args, **kwargs):
        if self.mpi_is_root:
            self.instance.create_chunk(idx, *args, **kwargs)
        self.mpi_barrier()
        return self.get_chunk(idx)

    def get_chunk(self, *args, **kwargs):
        chunk = self.instance.get_chunk(*args, **kwargs)
        return MpiDataChunk(chunk)

    def clear(self):
        for idx in range(self.mpi_rank, self.total_chunks, self.mpi_size):
            idx = self._idx_from_flat(idx)
            self.del_chunk(idx)
        self.mpi_barrier()

    def delete(self):
        self.mpi_barrier()
        if self.mpi_is_root:
            self.instance._pool.del_dataset(self.name)
        self.mpi_barrier()

    def load(self, data):
        if data.shape != self.shape:
            raise Exception('Data shape does not match')
        for idx in range(self.mpi_rank, self.total_chunks, self.mpi_size):
            idx = self._idx_from_flat(idx)

            gslices = self._global_chunk_bounds(idx)
            lslices = self._local_chunk_bounds(idx)
            self.set_chunk_data(idx, data[gslices], slices=lslices)
        self.mpi_barrier()

    def map(self, func, output_name):
        out = self.clone(output_name)
        for idx in range(self.mpi_rank, self.total_chunks, self.mpi_size):
            idx = self._idx_from_flat(idx)
            data = func(self.get_chunk_data(idx))
            out.set_chunk_data(idx, data)
        self.mpi_barrier()
        return out

    def apply(self, func):
        for idx in range(self.mpi_rank, self.total_chunks, self.mpi_size):
            idx = self._idx_from_flat(idx)
            data = func(self.get_chunk_data(idx))
            self.set_chunk_data(idx, data)
        self.mpi_barrier()

    def clone(self, output_name):
        if self.mpi_is_root:
            out = self.instance._pool.create_dataset(
                output_name, shape=self.shape,
                dtype=self.dtype, chunks=self.chunk_size,
                fillvalue=self.fillvalue)
        else:
            out = None
        out = self.mpi_comm.bcast(out, root=0)
        return MpiDataset(out, self.mpi_comm)


class MpiDataChunk(Wrapper):

    pass


# Export Engine
__engine__ = Engine('mpi', MpiCluster, MpiPool, MpiDataset, MpiDataChunk,
                    dict(comm=MPI.COMM_WORLD))
