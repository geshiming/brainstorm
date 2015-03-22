#!/usr/bin/env python
# coding=utf-8

from __future__ import division, print_function, unicode_literals
from copy import copy
import numpy as np
from brainstorm.handlers import default_handler


class ParameterView(tuple):
    def __new__(cls, structure, buffer):
        if structure:
            names, shapes = zip(*structure)
            bounds = list(np.cumsum([np.prod(shape) for shape in shapes]))
            vs = [buffer[start:stop].reshape(shape)
                  for start, stop, shape in zip([0] + bounds, bounds, shapes)]
        else:
            vs = ()
        instance = tuple.__new__(cls, vs)
        return instance

    def __init__(self, structure, buffer):
        super(ParameterView, self).__init__()
        if structure:
            names, shapes = zip(*structure)
            self._names = names
        else:
            self._names = ()
        self._buffer = buffer
        for i, (name, shape) in enumerate(structure):
            self.__dict__[name] = self[i]

    def _asdict(self):
        return dict(zip(self._names, self))

    def items(self):
        return self._asdict().items()

    def keys(self):
        return self._asdict().keys()

    def values(self):
        return self._asdict().values()

    def __getitem__(self, item):
        if isinstance(item, int):
            return super(ParameterView, self).__getitem__(item)
        return self.__dict__[item]


class ParameterBuffer(dict):
    """
    Handles the parameters of the network.
    The buffer is allocated at initialization, and the views for all the
    layers are created.
    """
    def __init__(self, param_layout, handler=default_handler):
        super(ParameterBuffer, self).__init__()
        self.size, self.layout = param_layout
        self.memory = None
        self.handler = handler

    def rearrange(self, memory):
        relocated = self._relocate_internal_memory(memory)
        if relocated:
            self._lay_out()

    def _relocate_internal_memory(self, memory):
        assert memory is not None, "No memory given to ParameterBuffer"

        if memory is self.memory:
            return False

        mem_size = memory.size
        assert mem_size == self.size, \
            "Given memory is wrong size: {} != {}".format(mem_size, self.size)
        self.memory = memory
        return True

    def _lay_out(self):
        for layer_name, layout_entry in self.layout.items():
            struct = layout_entry.structure
            buff = self.get_raw(layer_name)
            self[layer_name] = ParameterView(struct, buff)

    def __getitem__(self, item):
        if isinstance(item, slice):
            return self.memory[item]
        else:
            return dict.__getitem__(self, item)

    def get_raw(self, layer_name=None):
        """
        Get the part of the memory that corresponds to the given layer, or the
        the whole buffer if none is specified.
        """
        if layer_name is None:
            return self.memory
        else:
            start, stop, _ = self.layout[layer_name]
            return self.memory[start: stop]


class InOutBuffer(dict):
    """
    Handles input or output buffers. The memory is allocated on demand.
    There should always be one of this object for the inputs and one for the
    outputs with corresponding layouts that share the same memory region.
    """
    def __init__(self, hub_sizes, layouts, handler=default_handler):
        super(InOutBuffer, self).__init__()
        self.hub_sizes = hub_sizes
        self.size = 0
        self.layouts = layouts
        self.memory = None
        self.shape = None
        self.handler = handler

    def rearrange(self, shape, memory=None):
        shape_changed = self.shape != shape[:2]
        self.shape = shape[:2]
        self.size = self.get_size(self.shape)
        relocated = self._resize_internal_memory(memory)
        if relocated or shape_changed:
            self._lay_out()

    def get_size(self, shape):
        nr_timesteps, nr_sequences = shape[:2]
        return nr_timesteps * nr_sequences * sum(self.hub_sizes)

    def _resize_internal_memory(self, memory):
        if memory is None:
            assert self.memory is not None, "No memory found"
            assert self.memory.size >= self.size, "Insufficient Memory"
            return False

        if memory is self.memory:
            return False

        mem_size = memory.size
        assert mem_size >= self.size, \
            "Given memory is too small: {} < {}".format(mem_size, self.size)
        self.memory = memory
        return True

    def _lay_out(self):
        nr_timesteps, nr_sequences = self.shape
        i = 0
        for hub_feature_size, layout in zip(self.hub_sizes, self.layouts):
            hub_shape = (nr_timesteps, nr_sequences, hub_feature_size)
            hub_size = nr_timesteps * nr_sequences * hub_feature_size
            hub_buffer = self.memory[i:i + hub_size]
            hub_buffer = hub_buffer.reshape(hub_shape)
            i += hub_size
            for layer_name, feature_slice in layout.items():
                self[layer_name] = hub_buffer[:, :, feature_slice]


class BufferManager(object):
    # TODO needs refactor, because it essentially does everything twice
    def __init__(self, param_buffer, in_buffer, out_buffer,
                 handler=default_handler):
        self.parameters = param_buffer
        self.gradient = copy(param_buffer)
        self.inputs = in_buffer
        self.outputs = out_buffer
        self.in_deltas = copy(in_buffer)
        self.out_deltas = copy(out_buffer)
        self.fwd_shape = None
        self.bwd_shape = None
        self.param_memory = None
        self.grad_memory = None
        self.handler = handler
        self.fwd_memory = self.handler.EMPTY
        self.bwd_memory = self.handler.EMPTY

    def reset(self):
        self.fwd_shape = None
        self.bwd_shape = None
        self.param_memory = None
        self.grad_memory = None
        self.fwd_memory = self.handler.EMPTY
        self.bwd_memory = self.handler.EMPTY

    def set_memory_handler(self, handler):
        # remember the parameters
        params = None
        if self.param_memory is not None:
            params = self.handler.get(self.param_memory)
        self.reset()
        # set all handlers
        self.handler = handler
        self.parameters.handler = handler
        self.gradient.handler = handler
        self.inputs.handler = handler
        self.outputs.handler = handler
        self.in_deltas.handler = handler
        self.out_deltas.handler = handler
        # restore the parameters
        if params is not None:
            self.handler.set_from_numpy(self.param_memory, params)
            self.parameters.rearrange(self.param_memory)

    def rearrange_parameters(self):
        if self.param_memory is None:
            self.param_memory = self.handler.allocate(self.parameters.size)
            self.parameters.rearrange(self.param_memory)

    def rearrange_fwd(self, shape):
        """
        Resize the buffers needed for a foward pass and prepare them.
        :param shape: Tuple specifying the dimensions. Only the first two are
            used. They should be (nr_timesteps, nr_sequences).
        :type shape: tuple[int]
        """
        if self.fwd_shape == shape[:2]:
            return
        self.fwd_shape = shape[:2]

        in_size = self.inputs.get_size(self.fwd_shape)

        if self.fwd_memory.size < in_size:
            self.fwd_memory = self.handler.allocate(in_size)
            self.inputs.rearrange(self.fwd_shape, self.fwd_memory)
            self.outputs.rearrange(self.fwd_shape, self.fwd_memory)
        else:
            self.inputs.rearrange(self.fwd_shape)
            self.outputs.rearrange(self.fwd_shape)

    def rearrange_bwd(self):
        """
        Resize the buffers needed for a backward pass and prepare them.
        Reuses the same shape as for the forward pass.
        """
        if self.bwd_shape == self.fwd_shape:
            return
        self.bwd_shape = self.fwd_shape

        if self.grad_memory is None:
            self.grad_memory = self.handler.allocate(self.gradient.size)
            self.gradient.rearrange(self.grad_memory)

        deltas_size = self.in_deltas.get_size(self.bwd_shape)

        if self.handler.size(self.bwd_memory) < deltas_size:
            self.bwd_memory = self.handler.allocate(deltas_size)

            self.in_deltas.rearrange(self.bwd_shape, self.bwd_memory)
            self.out_deltas.rearrange(self.bwd_shape, self.bwd_memory)
        else:
            self.in_deltas.rearrange(self.bwd_shape)
            self.out_deltas.rearrange(self.bwd_shape)

    @classmethod
    def create_from_layers(cls, layers):
        #param_layout = create_param_layout(layers)
        #param_buffer = ParameterBuffer(param_layout)

        #buffer_hub_layouts = create_in_out_layout(layers)
        #hub_sizes, source_hubs, sink_hubs = zip(*buffer_hub_layouts)
        #out_buffer = InOutBuffer(hub_sizes, source_hubs)
        #in_buffer = InOutBuffer(hub_sizes, sink_hubs)
        #return cls(param_buffer, in_buffer, out_buffer)
        pass
