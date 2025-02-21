################################################################################
#
# Copyright (C) 2022-2024 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell cop-
# ies of the Software, and to permit persons to whom the Software is furnished
# to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IM-
# PLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNE-
# CTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
################################################################################

from .AsmAddressCalculation import AddrCalculation
from .Utils import DataDirection

from math import ceil, trunc, modf

##############################################################################
# StoreState
# tracks state that is preserved across globalWriteBatch calls:
# init is called before globalWriteBatch
# the KernelWriter object
##############################################################################
class StoreState:

    ##############################################################################
    # Setup store config for number of sgpr and vgpr needed
    # These are set based on edge, atomic, etc - do not change during
    # the generation of the store code.
    ##############################################################################
    class StoreConstConfig:
        def __init__(self, kernelWriter, kernel, ss, gwvw, edge, beta, atomic):
            self.gwvw = gwvw
            self.lsu = kernel["LocalSplitU"]

            if ss.optSingleColVgpr:
                # use one vgpr (allocated in ss.sharedColDVgprs) for all addressing
                # - need 0 additional vgpr per element.
                self.numVgprsPerAddr = 0
            else:
                self.numVgprsPerAddr = kernelWriter.states.rpgo if kernel["BufferStore"] else kernelWriter.states.rpga

            if ss.optSGPRUsage == 'BufferLoad_Mask':
                self.numMaskSgprPerElement = 0
                self.numMaskSgprPerBatch   = 0
                self.numTempSgprPerBatch   = kernelWriter.states.laneSGPRCount
            elif ss.optSGPRUsage == 'BufferLoad_Edge_Mask':
                self.numMaskSgprPerElement = 0
                self.numMaskSgprPerBatch   = kernelWriter.states.laneSGPRCount
                self.numTempSgprPerBatch   = 2 * kernelWriter.states.laneSGPRCount
            else:
                self.numMaskSgprPerElement = kernelWriter.states.laneSGPRCount
                self.numMaskSgprPerBatch   = 0
                self.numTempSgprPerBatch   = 2 * kernelWriter.states.laneSGPRCount

            if self.numMaskSgprPerElement:
                numSgprAvailable = kernelWriter.consts.maxSgprs - kernelWriter.sgprPool.size() + kernelWriter.sgprPool.availableBlockAtEnd()
                numSgprAvailable = numSgprAvailable & ~0x1 # make sure it's aligned
                #print("numSgprAvailable=", numSgprAvailable)
                self.numElementsPerBatchLimitedBySgprs = (numSgprAvailable - self.numTempSgprPerBatch - self.numMaskSgprPerBatch) // self.numMaskSgprPerElement
            else:
                self.numElementsPerBatchLimitedBySgprs = 9999 # no limit

            if self.numElementsPerBatchLimitedBySgprs<=0:
                kernelWriter.overflowedResources = 2
                self.numElementsPerBatchLimitedBySgprs = 1 # dummy value
                  #assert self.numElementsPerBatchLimitedBySgprs > 0, "numElementsPerBatchLimitedBySgprs=0 for %s"%self.kernelName

            if atomic:
                # flat atomics have another VGPR to allow different data for return#
                regsPerElement = 2 if kernel["BufferStore"] else (3 + 1) # + 1 for alignment
                # The atomic loop processes multiple elements in single instruction
                # so will use VGPR from consec elements? TODO
                self.numVgprsPerDataPerVI = (1.0 * regsPerElement * kernelWriter.states.bpeCexternal) / kernelWriter.states.bpr
            elif beta:
                self.numVgprsPerDataPerVI = (1.0 * kernelWriter.states.bpeCexternal) / kernelWriter.states.bpr
            else:
                self.numVgprsPerDataPerVI = 0.0

            if kernelWriter.states.serializedStore:
                #self.numVgprPerValuC = kernel["MIRegPerOut"]
                self.numVgprPerValuC = kernelWriter.states.bpeCinternal//kernelWriter.states.bpr # vgpr needed from register pool
            else:
                self.numVgprPerValuC = 0 # null since they are already declared in macro part of assembly kernel

            # indicates each vector element is actually half -
            # changes vgpr allocation so two elements share a data vgpr
            # Really only used if gwvw=1 - edge cases
            # exception: data vgpr cannot be shared if UseInitialStridesCD is enabled and card enable EccHalf,
            #            since each buffer_load_short would overwrite undefined 16bit as zero.
            self.halfDataRegPerVI = gwvw*self.numVgprsPerDataPerVI == 0.5 and not (kernel["ProblemType"]["UseInitialStridesCD"] and kernelWriter.states.archCaps["HasEccHalf"]) and not (kernel["ProblemType"]["DestDataType"].numRegisters() == 0.25)

    # StoreState constructor:
    def __init__(self, kernelWriter, kernel, gwvw, edge, beta, atomic, elements):
        self.kernelWriter = kernelWriter
        self.kernel = kernel
        self.lsu = kernel["LocalSplitU"]

        self.isReset = False
        #--
        # Optimizations for coord0/column address calculations:
        #
        # optSingleColVgpr:
        #  - works in cases where the data is written row by row to memory.
        # In this case we can use a single vgpr for addressing:
        #  - Use the load/store instruction offset (fixed at compile-time)
        #  - the horizontal addresses are fixed offsets from the base
        #  - as we move to a new row, increment the appropriate SRDs

        # optSharedColVgpr:
        #  - Each col gets it's own address, but elements in later rows with the same col will share VGPR.
        #  - allows cols to be non-adjacent
        #  - this is mutually exclusive with optSingleColVgpr - not as optimal but provides
        #    more flexibility.

        # optSrdIncForRow: optimize coord1/row address calculations:
        #  - Move the SRD between memory operations to get to new row
        #    atomic needs to reset the SRD to handle retry loop.  Then might work.

        self.optSingleColVgpr = 0
        self.optSharedColVgpr = 0
        self.optSrdIncForRow  = 0

        # opt*ColVgpr doesn't work for edge since each element requires own addr VGPR so
        #    we can perform bounds check and set to -1 for OOB accesses.
        # if optSingleColVgpr = optSharedColVgpr = 0, then each element gets
        #  1-2 VGPRs to track address.  Address calcs are performed independently
        #  for each element.

        # atomic contains multiple memory operations which need to preserve
        # the address for each load.  Memops in same row can use offsets
        # and share a base register but Memops in different rows need
        # different registers or need to inteligently reset the SRD.
        if kernel["BufferStore"] and not edge and not atomic:
            if len(kernel["PackedC0IndicesX"]) > 1:
                # packed mode needs a unique VGPR address calc for each column.
                self.optSharedColVgpr = 1
            elif len(kernel["PackedC1IndicesX"]) > 1:
                self.optSharedColVgpr = 0
                self.optSingleColVgpr = 0
            else:
                self.optSingleColVgpr = 1

            if not atomic and len(kernel["PackedC1IndicesX"]) == 1:
                self.optSrdIncForRow = 1

        if kernel["StoreRemapVectorWidth"]:
            self.optSrdIncForRow = 1

        if kernel["ProblemType"]["UseInitialStridesCD"]:
            self.optSingleColVgpr = 0 # BOZO, hack to disable this
            self.optSharedColVgpr = 0# BOZO, hack to disable this

        self.optSGPRUsage = None
        if kernel["BufferStore"] and (not atomic):
            self.optSGPRUsage = 'BufferLoad_Edge_Mask' if edge else 'BufferLoad_Mask'

        # can't have both of these enabled:
        assert (not (self.optSingleColVgpr and self.optSharedColVgpr))


        self.cfg = self.StoreConstConfig(kernelWriter, kernel, self, gwvw, edge, beta, atomic)

        # Use to detect new rows:
        self.lastCoordOffset1 = 0

        # vgpr holding current coord, setup initial state
        self.coord1Vgpr = kernelWriter.vgprs.coord1

        # epilogue related
        self.useBias = kernelWriter.states.useBias

        if self.optSharedColVgpr:
            numCols = len([e for e in elements if e[0] == 0 and e[2] == 0]) # count #elements with row d1=v1==0
            self.numAddrVgpr = numCols
            self.sharedColDVgprs = kernelWriter.vgprPool.checkOut(self.numAddrVgpr, "sharedColDVgprs for packed elements")
            if kernel["GroupLoadStore"] and kernel["ProblemType"]["UseBeta"]:
                self.sharedColCVgprs = kernelWriter.vgprPool.checkOut(self.numAddrVgpr, "sharedColCVgprs for packed elements")
            else:
                self.sharedColCVgprs = self.sharedColDVgprs
            if self.useBias != DataDirection.NONE:
                self.sharedColBiasVgprs = kernelWriter.vgprPool.checkOut(self.numAddrVgpr, "sharedColBiasVgprs for packed elements")
            else:
                self.sharedColBiasVgprs = None
            if kernel["ProblemType"]["UseE"] and (kernel["GlobalSplitU"] == 1):
                self.sharedColEVgprs = kernelWriter.vgprPool.checkOut(self.numAddrVgpr, "sharedColEVgprs for packed elements")
            else:
                self.sharedColEVgprs = None
            if kernel["ProblemType"]["UseScaleAlphaVec"] and (kernel["GlobalSplitU"] == 1):
                self.sharedColScaleAlphaVecVgprs = kernelWriter.vgprPool.checkOut(self.numAddrVgpr, "sharedColScaleAlphaVecVgprs for packed elements")
            else:
                self.sharedColScaleAlphaVecVgprs = None
        elif self.optSingleColVgpr:
            self.numAddrVgpr = 1
            self.sharedColDVgprs = kernelWriter.vgprPool.checkOut(1, "sharedColDVgprs")
            self.singleColBiasAddrUpdated = False
            self.singleColEAddrUpdated    = False
            self.singleColDAddrUpdated    = False
            self.singleColCAddrUpdated    = False
            if kernel["ProblemType"]["UseBeta"]:
                self.sharedColCVgprs = kernelWriter.vgprPool.checkOut(1, "sharedColCVgprs")
            else:
                self.sharedColCVgprs = self.sharedColDVgprs
            if self.useBias != DataDirection.NONE:
                self.sharedColBiasVgprs = kernelWriter.vgprPool.checkOut(1, "sharedColBiasVgprs for packed elements")
            else:
                self.sharedColBiasVgprs = None
            if kernel["ProblemType"]["UseE"] and (kernel["GlobalSplitU"] == 1):
                self.sharedColEVgprs = kernelWriter.vgprPool.checkOut(1, "sharedColEVgprs for packed elements")
            else:
                self.sharedColEVgprs = None
            if kernel["ProblemType"]["UseScaleAlphaVec"] and (kernel["GlobalSplitU"] == 1):
                self.sharedColScaleAlphaVecVgprs = kernelWriter.vgprPool.checkOut(1, "sharedColScaleAlphaVecVgprs for packed elements")
            else:
                self.sharedColScaleAlphaVecVgprs = None
        else:
            self.numAddrVgpr = 0
            self.sharedColEVgprs    = None
            self.sharedColDVgprs    = None
            self.sharedColCVgprs    = None
            self.sharedColBiasVgprs = None
            self.sharedColScaleAlphaVecVgprs = None

        # For detecting when we are running first batch
        self.firstBatch = True

        # Calculate numVgprsPerElement
        # print("numVgprsPerAddr=%u, numVgprsPerDataPerVI=%u, numVgprPerValuC=%u"%(self.cfg.numVgprsPerAddr, self.cfg.numVgprsPerDataPerVI, self.cfg.numVgprPerValuC))
        self.numVgprsPerElement = self.cfg.numVgprPerValuC*gwvw + self.cfg.numVgprsPerAddr + int(ceil(self.cfg.numVgprsPerDataPerVI * gwvw))
        if kernel["GroupLoadStore"] and kernel["ProblemType"]["UseBeta"]:
            self.numVgprsPerElement += self.cfg.numVgprsPerAddr
        if kernel["ProblemType"]["UseE"] and (kernel["GlobalSplitU"] == 1):
            self.numVgprsPerElement += self.cfg.numVgprsPerAddr  # E address
            # Only needed in gradient activation
            if (kernel["ProblemType"]["Gradient"] and kernel["ProblemType"]["ActivationType"] != 'none'):
                numVgprs = int(ceil(kernel["ProblemType"]["ComputeDataType"].numRegisters()))
                self.numVgprsPerElement += numVgprs * gwvw # Loaded data
        if self.useBias != DataDirection.NONE:
            self.numVgprsPerElement += self.cfg.numVgprsPerAddr  # Bias address
            if self.useBias == DataDirection.READ:
                numVgprs = int(ceil(kernel["ProblemType"]["ComputeDataType"].numRegisters()))
                self.numVgprsPerElement += numVgprs * gwvw  # Loaded data

        if kernel["ProblemType"]["UseScaleAlphaVec"] and (kernel["GlobalSplitU"] == 1):
            self.numVgprsPerElement += self.cfg.numVgprsPerAddr  # ScaleAlphaVec address
            numVgprs = int(ceil(kernel["ProblemType"]["DataType"].numRegisters()))
            self.numVgprsPerElement += numVgprs * gwvw  # Loaded data
        # Calculate align
        self.align = 1
        # align adjustment
        if self.cfg.numVgprsPerAddr > 1:
            self.align = max(self.align, self.cfg.numVgprsPerAddr)
        if self.cfg.numVgprPerValuC*gwvw > 1:
            self.align = max(self.align, self.cfg.numVgprPerValuC*gwvw)
        if int(ceil(self.cfg.numVgprsPerDataPerVI * gwvw)) > 1:
            self.align = max(self.align, int(ceil(self.cfg.numVgprsPerDataPerVI * gwvw)))

    ##############################################################################
    # Setup data structures to feed store loops:
    #   self.elementAddr, self.elementData, self.elementMask, self.elementSumIdx
    # batchElements is a list of (d0,d1,v0,v1) for which stores to perform
    # batchElementSgprs is SGPRs to use for mask.  If None, elementMask is
    #  not initialized.
    #
    # Also create an AddrCalc for each memory operation.
    ##############################################################################
    def setupStoreElementsForBatch(self, kernel, gwvw, batchElements, batchElementSgprs, isOptNLL, biasDim):

        self.elementAddr     = []
        self.elementDataE    = []
        self.elementData     = []  # VGPR to use for element data, needed for atomic or beta
        self.elementDataBias = []
        self.elementDataScaleAlphaVec = []
        self.elementMask     = []  # SGPR to use for element mask
        self.elementSumIdx = []

        kw = self.kernelWriter

        if kernel["EnableMatrixInstruction"]:
            matrixInstM  = (kernel["MatrixInstM"] * kernel["MatrixInstBM"]) if (kernel["MatrixInstM"] == 4) else kernel["MatrixInstM"]
            matrixInstN  = (kernel["MatrixInstN"] * kernel["MatrixInstBN"]) if (kernel["MatrixInstN"] == 4) else kernel["MatrixInstN"]
            matrixInstBM = 1                                                if (kernel["MatrixInstM"] == 4) else kernel["MatrixInstBM"]
            matrixInstBN = 1                                                if (kernel["MatrixInstN"] == 4) else kernel["MatrixInstBN"]

        biasVgprMap = {}
        scaleAlphaVecVgprMap = {}
        lastData = 0
        for elementIdx in range(0, len(batchElements)):
            # Create the AddrCalc for each memory load/store
            # This is the control code that sets up the dest, source, offsets, etc and
            # identifies cases where the AddrCalc is a new row and therefore needs some
            # additional math.  Each AddrCalc contains isolated state sufficient to
            # perform any needed range checks and address calculations for the element.
            #
            # The AddrCalc creation code here maintains state across elements (including
            # across write batches) to remove replicated calculations.
            #
            # Later the AddrCalc::emitAddressSetupCode will emit the necessary code
            # Also allocate VGPR resources here, if needed.

            element = batchElements[elementIdx]
            (d1,d0,vc1,vc0) = element

            coordOffset1 = 0
            if kernel["EnableMatrixInstruction"]:
                vectorWidth = kernel["VectorWidthB"] # TODO: nonSwap VectorWidth
                MIOutputVectorWidth = kernel["MIOutputVectorWidth"]
                MFMAContinuousOutputs = MIOutputVectorWidth if kernel["SourceSwap"] else 1
                OutputsPerMIMN        = (matrixInstM * matrixInstN // self.kernel["WavefrontSize"]) if kernel["SourceSwap"] else 1

                eIdx1        = d1 % (OutputsPerMIMN // MFMAContinuousOutputs)
                remain_d1    = d1 // (OutputsPerMIMN // MFMAContinuousOutputs)

                bIdx1     = remain_d1 % matrixInstBN
                remain_d1 = remain_d1 // matrixInstBN
                wtIdex    = remain_d1 % kernel["MIWaveTile"][1]

                coordOffset1  = eIdx1 * (self.kernel["WavefrontSize"] // matrixInstN) * MFMAContinuousOutputs
                coordOffset1 += bIdx1 * matrixInstN
                coordOffset1 += wtIdex * matrixInstN *  matrixInstBN * kernel["MIWaveGroup"][1]
                coordOffset1  = coordOffset1 * vectorWidth + vc1

                # LSU part
                if kernel["LocalSplitU"] == 2 and kernel["SourceSwap"]:
                    # if LSU==4, we have exact 4 group per MI output. Hence, no offset needed.
                    if kernel["NumGlobalWriteVectorsPerThread"] >= kernel["LocalSplitU"]:
                        lsu_d1 = d1 // (kernel["NumGlobalWriteVectorsPerThread"] // kernel["LocalSplitU"])
                        lsuStep = OutputsPerMIMN // kernel["LocalSplitU"]
                        coordOffset1 += lsu_d1 * lsuStep

            newCoord1 = (self.firstBatch and elementIdx==0) or (coordOffset1 != self.lastCoordOffset1)

            # gpr and offset assignments for element
            coordOffset0 = 0
            if kernel["EnableMatrixInstruction"]:
                vectorWidth = kernel["VectorWidthA"]
                MFMAContinuousOutputs = 1 if kernel["SourceSwap"] else kernel["MIOutputVectorWidth"]
                OutputsPerMIMN        = 1 if kernel["SourceSwap"] else matrixInstM * matrixInstN // self.kernel["WavefrontSize"]

                eIdx0        = d0 % (OutputsPerMIMN // MFMAContinuousOutputs)
                remain_d0    = d0 // (OutputsPerMIMN // MFMAContinuousOutputs)

                bIdx0        = remain_d0 % matrixInstBM
                remain_d0    = remain_d0 // matrixInstBM
                wtIdex       = remain_d0 % kernel["MIWaveTile"][0]

                coordOffset0  = eIdx0 * (self.kernel["WavefrontSize"] // matrixInstM) * MFMAContinuousOutputs
                coordOffset0 += bIdx0 * matrixInstM
                coordOffset0 += wtIdex * matrixInstM * matrixInstBM * kernel["MIWaveGroup"][0]
                coordOffset0  = coordOffset0 * vectorWidth + vc0

                # LSU part
                if kernel["LocalSplitU"] == 2 and not kernel["SourceSwap"]:
                    # if LSU==4, we have exact 4 group per MI output. Hence, no offset needed.
                    if kernel["NumGlobalWriteVectorsPerThread"] >= kernel["LocalSplitU"]:
                        lsu_d1 = d0 // (kernel["NumGlobalWriteVectorsPerThread"] // kernel["LocalSplitU"])
                        lsuStep = OutputsPerMIMN // kernel["LocalSplitU"]
                        coordOffset0 += lsu_d1 * lsuStep

            if self.optSingleColVgpr:
                # use same address vgpr for all
                addrEVgpr    = self.sharedColEVgprs
                addrDVgpr    = self.sharedColDVgprs
                addrCVgpr    = self.sharedColCVgprs
                addrBiasVgpr = self.sharedColBiasVgprs
                addrScaleAlphaVecVgpr = self.sharedColScaleAlphaVecVgprs
            elif self.optSharedColVgpr:
                if kernel["EnableMatrixInstruction"]:
                    elementCol = (d0 * kernel["MIOutputVectorWidth"] + vc0) / gwvw
                else:
                    elementCol = (d0 * kernel["VectorWidthA"] + vc0) / gwvw
                assert (modf(elementCol)[0] < 0.001)
                elementCol   = trunc(elementCol)
                addrDVgpr    = self.sharedColDVgprs+elementCol
                addrCVgpr    = self.sharedColCVgprs+elementCol
                if self.useBias != DataDirection.NONE:
                    addrBiasVgpr = self.sharedColBiasVgprs+elementCol
                else:
                    addrBiasVgpr = None
                if kernel["ProblemType"]["UseE"] and (kernel["GlobalSplitU"] == 1):
                    addrEVgpr = self.sharedColEVgprs+elementCol
                else:
                    addrEVgpr = None
                #print ("d0=", d0, "vc0=", vc0, "elementCol=", elementCol)

                if kernel["ProblemType"]["UseScaleAlphaVec"] and (kernel["GlobalSplitU"] == 1):
                    addrScaleAlphaVecVgpr = self.sharedColScaleAlphaVecVgprs+elementCol
                else:
                    addrScaleAlphaVecVgpr = None

            else:
                # allocate new VGPR for each element:
                addrDVgpr = kw.vgprPool.checkOutAligned(self.cfg.numVgprsPerAddr, \
                    int(ceil(self.cfg.numVgprsPerAddr)), "writeDBatch-addr for ei=%u"%(elementIdx), preventOverflow=not isOptNLL)
                if kernel["GroupLoadStore"] and kernel["ProblemType"]["UseBeta"]:
                    addrCVgpr = kw.vgprPool.checkOutAligned(self.cfg.numVgprsPerAddr, \
                        int(ceil(self.cfg.numVgprsPerAddr)), "loadCBatch-addr for ei=%u"%(elementIdx), preventOverflow=not isOptNLL)
                else:
                    addrCVgpr = addrDVgpr
                if self.useBias != DataDirection.NONE:
                    addrBiasVgpr = kw.vgprPool.checkOutAligned(self.cfg.numVgprsPerAddr, \
                        int(ceil(self.cfg.numVgprsPerAddr)), "loadBiasBatch-addr for ei=%u"%(elementIdx), preventOverflow=not isOptNLL)
                else:
                    addrBiasVgpr = None
                if kernel["ProblemType"]["UseE"] and (kernel["GlobalSplitU"] == 1):
                    addrEVgpr = kw.vgprPool.checkOutAligned(self.cfg.numVgprsPerAddr, \
                        int(ceil(self.cfg.numVgprsPerAddr)), "loadEBatch-addr for ei=%u"%(elementIdx), preventOverflow=not isOptNLL)
                else:
                    addrEVgpr = None

                if kernel["ProblemType"]["UseScaleAlphaVec"] and (kernel["GlobalSplitU"] == 1):
                    addrScaleAlphaVecVgpr = kw.vgprPool.checkOutAligned(self.cfg.numVgprsPerAddr, \
                        int(ceil(self.cfg.numVgprsPerAddr)), "loadScaleAlphaVecBatch-addr for ei=%u"%(elementIdx), preventOverflow=not isOptNLL)
                else:
                    addrScaleAlphaVecVgpr = None
            self.elementAddr.append(AddrCalculation(kw, self, addrCVgpr, addrDVgpr, addrEVgpr, addrBiasVgpr, addrScaleAlphaVecVgpr, element, coordOffset0, \
              self.kernelWriter.vgprs.coord1, coordOffset1, coordOffset1 - self.lastCoordOffset1, newCoord1))
            # if numVgprsPerDataPerVI == 0.5, then two consecutive elements
            # should have same data pointer, next should move.

            if self.cfg.numVgprsPerDataPerVI > 0:
                if self.cfg.halfDataRegPerVI:
                    # TODO- check (H,H,H,H,S,S)
                    if kernel["ProblemType"]["HighPrecisionAccumulate"] and \
                       (kernel["ProblemType"]["DestDataType"].isBFloat16() or kernel["ProblemType"]["DestDataType"].isHalf()):
                        data = kw.vgprPool.checkOutAligned(int(2*self.cfg.numVgprsPerDataPerVI*self.cfg.gwvw), \
                              int(ceil(int(2*self.cfg.numVgprsPerDataPerVI*self.cfg.gwvw))), "writeBatch-data for ei=%u and ei=%u"%(elementIdx,elementIdx+1), preventOverflow=not isOptNLL)
                    else:
                        if elementIdx%2 == 0:
                            # allocate for two elements:
                            data = kw.vgprPool.checkOutAligned(int(2*self.cfg.numVgprsPerDataPerVI*self.cfg.gwvw), \
                                   int(ceil(int(2*self.cfg.numVgprsPerDataPerVI*self.cfg.gwvw))), "writeBatch-data for ei=%u and ei=%u"%(elementIdx,elementIdx+1), preventOverflow=not isOptNLL)
                            lastData = data
                        else:
                            data = lastData
                            del lastData
                else:
                    if self.cfg.numVgprsPerDataPerVI == 0.5 or self.cfg.numVgprsPerDataPerVI == 0.25:
                        data = kw.vgprPool.checkOutAligned(int(ceil(self.cfg.numVgprsPerDataPerVI*self.cfg.gwvw)), \
                              int(ceil(self.cfg.numVgprsPerDataPerVI*self.cfg.gwvw)), "writeBatch-data for ei=%u"%elementIdx, preventOverflow=False)
                    else:
                        data = kw.vgprPool.checkOutAligned(int(self.cfg.numVgprsPerDataPerVI*self.cfg.gwvw), \
                              int(ceil(self.cfg.numVgprsPerDataPerVI*self.cfg.gwvw)), "writeBatch-data for ei=%u"%elementIdx, preventOverflow=False)
                    #data = kw.vgprPool.checkOut(int(self.cfg.numVgprsPerDataPerVI*self.cfg.gwvw), \
                    #      "writeBatch-data for ei=%u"%elementIdx, preventOverflow=False)
            else:
                data = 0

            self.elementData.append(data)

            if self.useBias == DataDirection.READ:
                coordOffset = coordOffset0 if biasDim == 0 else coordOffset1
                if coordOffset in biasVgprMap:
                    dataBias = biasVgprMap[coordOffset]
                else:
                    numVgprs = int(ceil(kernel["ProblemType"]["ComputeDataType"].numRegisters()))
                    dataBias = kw.vgprPool.checkOutAligned(int(numVgprs*self.cfg.gwvw), \
                                int(ceil(numVgprs*self.cfg.gwvw)), "bias data for ei=%u"%elementIdx, preventOverflow=False)
                    biasVgprMap[coordOffset] = dataBias
            else:
                dataBias = 0
            self.elementDataBias.append(dataBias)

            # Only needed in gradient activation
            if (kernel["ProblemType"]["Gradient"] and kernel["ProblemType"]["ActivationType"] != 'none' and kernel["ProblemType"]["UseE"]) and (kernel["GlobalSplitU"] == 1):
                numVgprs = int(ceil(kernel["ProblemType"]["ComputeDataType"].numRegisters()))
                dataE = kw.vgprPool.checkOutAligned(int(numVgprs*self.cfg.gwvw), \
                              int(ceil(numVgprs*self.cfg.gwvw)), "e data for ei=%u"%elementIdx, preventOverflow=False)
            else:
                dataE = 0
            self.elementDataE.append(dataE)

            if kernel["ProblemType"]["UseScaleAlphaVec"] and (kernel["GlobalSplitU"] == 1):
                if coordOffset0 in scaleAlphaVecVgprMap:
                    dataScaleAlphaVec = scaleAlphaVecVgprMap[coordOffset0]
                else:
                    numVgprs = int(ceil(kernel["ProblemType"]["ComputeDataType"].numRegisters()))
                    dataScaleAlphaVec = kw.vgprPool.checkOutAligned(int(numVgprs*self.cfg.gwvw), \
                                  int(ceil(numVgprs*self.cfg.gwvw)), "scaleAlphaVec data for ei=%u"%elementIdx, preventOverflow=False)
                    scaleAlphaVecVgprMap[coordOffset0] = dataScaleAlphaVec
            else:
                dataScaleAlphaVec = 0
            self.elementDataScaleAlphaVec.append(dataScaleAlphaVec)

            if batchElementSgprs != None:
                if self.optSGPRUsage:
                    mask = batchElementSgprs
                else:
                    mask = batchElementSgprs + self.cfg.numMaskSgprPerBatch + elementIdx * self.cfg.numMaskSgprPerElement
                self.elementMask.append(mask)

            #print "Edge=", edge, element
            sumIdx = 0
            if kernel["LocalSplitU"] > 1:
                sumIdx = kw.states.c.startVgprValu + vc0 + d1*kernel["VectorWidthA"]
            else:
                bestVw                  = kernel["VectorWidthA"]
                elementsLoadedPerVw     = kernel["NumThreads"] * bestVw
                elementsLoadedPerbestVw = kernel["NumThreads"] * kernel["StoreVectorWidth"]

                if elementsLoadedPerVw < elementsLoadedPerbestVw:
                    bestVw = kernel["StoreVectorWidth"]

                if kernel["EnableMatrixInstruction"]:
                    alignment = self.cfg.numVgprPerValuC * self.cfg.gwvw
                    sumIdx    = kw.vgprPool.checkOutAligned(self.cfg.numVgprPerValuC*self.cfg.gwvw, alignment, "vgprValuC") // self.cfg.numVgprPerValuC
                else:
                    sumIdx = kw.states.c.startVgprValu + vc0 + d0*kernel["VectorWidthA"] + vc1*kernel["ThreadTile0"] + d1*kernel["VectorWidthA"]*kernel["ThreadTile0"]
            self.elementSumIdx.append(sumIdx) # sumIdx is an element idx, need to div/2 for half
            self.lastCoordOffset1 = coordOffset1
        # reset flag
        self.isReset = False

    def checkInTempVgprC(self):
        if self.kernelWriter.states.serializedStore is False:
            return # early exit; currently only serializedStore==True checks out C-tile from register pool

        if len(self.elementSumIdx) > 0 and self.lsu == 1:
            for i in self.elementSumIdx:
                self.kernelWriter.vgprPool.checkIn(i * self.cfg.numVgprPerValuC)
                # print("checked in vgpr %u"%i)
            self.elementSumIdx = []

    def resetState(self):
        if not self.isReset:
            # Init part
            if self.optSharedColVgpr:
                pass # Nothing to reset
            elif self.optSingleColVgpr:
                self.singleColBiasAddrUpdated = False
                self.singleColEAddrUpdated    = False
                self.singleColDAddrUpdated    = False
                self.singleColCAddrUpdated    = False
            else:
                pass # Nothing to reset
            # setup store element
            self.lastCoordOffset1 = 0
            self.isReset = True

    def __del__(self):

        if (self.sharedColEVgprs != None):
            self.kernelWriter.vgprPool.checkIn(self.sharedColEVgprs)
            self.sharedColEVgprs = None
        if (self.sharedColDVgprs != None):
            self.kernelWriter.vgprPool.checkIn(self.sharedColDVgprs)
            if (self.sharedColCVgprs != self.sharedColDVgprs and self.sharedColCVgprs != None):
                self.kernelWriter.vgprPool.checkIn(self.sharedColCVgprs)
                self.sharedColCVgprs = None
            self.sharedColDVgprs = None
        if (self.sharedColBiasVgprs != None):
            self.kernelWriter.vgprPool.checkIn(self.sharedColBiasVgprs)
            self.sharedColBiasVgprs = None
        if (self.sharedColScaleAlphaVecVgprs != None):
            self.kernelWriter.vgprPool.checkIn(self.sharedColScaleAlphaVecVgprs)
            self.sharedColScaleAlphaVecVgprs = None
        self.checkInTempVgprC()
        self.resetState()
