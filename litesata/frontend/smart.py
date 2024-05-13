#
# This file is part of LiteSATA.
#
# Copyright (c) 2023 Ryohei Niwase <niwase@lila.cs.tsukuba.ac.jp>
# SPDX-License-Identifier: BSD-2-Clause

from math import log2

from litesata.common import *

from litex.gen import *
from litex.soc.interconnect.csr import *
from litex.soc.cores.dma import WishboneDMAWriter

# LiteSATASMARTReadData ----------------------------------------------------------------------------

class LiteSATASMARTReadData(Module, AutoCSR):
    def __init__(self, user_port, with_csr=True):
        self.start      = Signal()
        self.done       = Signal()
        self.data_width = user_port.dw

        fifo = ResetInserter()(stream.SyncFIFO([("data", 32)], 128, buffered=True))
        self.submodules += fifo
        self.source = fifo.source

        # # #

        source, sink = user_port.sink, user_port.source

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            self.done.eq(1),
            If(self.start,
                NextState("SEND-CMD")
            )
        )
        self.comb += [
            source.last.eq(1),
            source.smart_read_data.eq(1),
        ]
        fsm.act("SEND-CMD",
            fifo.reset.eq(1),
            source.valid.eq(1),
            If(source.valid & source.ready,
                NextState("WAIT-ACK")
            )
        )
        fsm.act("WAIT-ACK",
            If(sink.valid & sink.smart_read_data,
                NextState("RECEIVE-DATA")
            )
        )
        self.comb += fifo.sink.data.eq(sink.data)
        fsm.act("RECEIVE-DATA",
            sink.ready.eq(fifo.sink.ready),
            If(sink.valid,
                fifo.sink.valid.eq(1),
                If(sink.last,
                    NextState("IDLE")
                )
            )
        )

        if with_csr:
            self.add_csr()

    def add_csr(self):
        self._start        = CSR()
        self._done         = CSRStatus()
        self._source_valid = CSRStatus()
        self._source_ready = CSR()
        self._source_data  = CSRStatus(32)

        # # #

        self.comb += [
            self.start.eq(self._start.r & self._start.re),
            self._done.status.eq(self.done),

            self._source_valid.status.eq(self.source.valid),
            self._source_data.status.eq(self.source.data),
            self.source.ready.eq(self._source_ready.r & self._source_ready.re)
        ]

# LiteSATASMARTReadDataDMA -------------------------------------------------------------------------

class LiteSATASMARTReadDataDMA(Module, AutoCSR):
    def __init__(self, port, bus, endianness="little"):
        self.port     = port
        self.bus      = bus
        self.base     = CSRStorage(64)
        self.start    = CSR()
        self.done     = CSRStatus()
        self.error    = CSRStatus()
        self.irq      = Signal()

        # # #

        port_bytes = port.dw//8
        dma_bytes  = bus.data_width//8
        count      = Signal(max=logical_sector_size//dma_bytes)
        crt_base   = Signal(64)

        # Sector buffer
        buf = stream.SyncFIFO([("data", port.dw)], logical_sector_size//port_bytes)
        self.submodules.buf = buf

        # Converter
        conv = stream.Converter(nbits_from=port.dw, nbits_to=bus.data_width)
        self.submodules.conv = conv

        # Connect Port to Sector Buffer
        self.comb += port.source.connect(buf.sink, keep={"valid", "ready", "last", "data"})

        # Connect Sector Buffer to Converter
        self.comb += buf.source.connect(conv.sink)

        # DMA
        dma = WishboneDMAWriter(bus, with_csr=False, endianness=endianness)
        self.submodules.dma = dma

        # Control FSM
        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(self.start.re,
                NextValue(count,             0),
                NextValue(crt_base,          self.base.storage),
                NextValue(self.error.status, 0),
                NextState("SEND-CMD")
            ).Else(
                self.done.status.eq(1)
            ),
            conv.source.ready.eq(1)
        )
        fsm.act("SEND-CMD",
            port.sink.valid.eq(1),
            port.sink.last.eq(1),
            port.sink.smart_read_data.eq(1),
            If(port.sink.ready,
                NextState("RECEIVE-DATA-DMA")
            )
        )
        fsm.act("RECEIVE-DATA-DMA",
            # Connect Converter to DMA.
            dma.sink.valid.eq(conv.source.valid),
            dma.sink.last.eq(conv.source.last),
            dma.sink.address.eq(crt_base[int(log2(dma_bytes)):] + count),
            dma.sink.data.eq(reverse_bytes(conv.source.data)),
            conv.source.ready.eq(dma.sink.ready),
            If(dma.sink.valid & dma.sink.ready,
                NextValue(count, count + 1),
                If(dma.sink.last,
                    self.irq.eq(1),
                    NextState("IDLE")
                )
            ),

            # Monitor errors
            If(port.source.valid & port.source.ready & port.source.failed,
                self.irq.eq(1),
                NextValue(self.error.status, 1),
                NextState("IDLE"),
            )
        )
