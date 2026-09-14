module FprimeSoakTestReference {

  # ----------------------------------------------------------------------
  # Symbolic constants for port numbers
  # ----------------------------------------------------------------------

  enum Ports_RateGroups {
    rateGroup1KHz
    rateGroup10Hz
    rateGroup1Hz
  }

  deployment topology FprimeSoakTestReferenceDeployment {

  # ----------------------------------------------------------------------
  # Subtopology imports
  # ----------------------------------------------------------------------
    import CdhCore.Subtopology
    # Space-packet layer only (no TM/TC transfer frames). Rfm69Manager is the
    # sole Svc.Com adapter for the packet radio (no TCP / ComStub).
    import ComCcsds.SpacePacketFraming
    import DataProducts.Subtopology
    import FileHandling.Subtopology
    import MpuImu.Subtopology
    import Bmp280.Subtopology
    import Rfm69.Subtopology

  # ----------------------------------------------------------------------
  # Instances used in the topology
  # ----------------------------------------------------------------------
    instance chronoTime
    instance rateGroup1KHz
    instance rateGroup10Hz
    instance rateGroup1Hz
    instance rateGroupDriver
    instance systemResources
    instance timer
    instance cmdSeq
    instance comRetry
    instance sensorDataProducer

  # ----------------------------------------------------------------------
  # Pattern graph specifiers
  # ----------------------------------------------------------------------

    command connections instance CdhCore.cmdDisp
    event connections instance CdhCore.events
    telemetry connections instance CdhCore.tlmSend
    text event connections instance CdhCore.textLogger
    health connections instance CdhCore.$health
    param connections instance FileHandling.prmDb
    time connections instance chronoTime

  # ----------------------------------------------------------------------
  # Telemetry packets (only used when TlmPacketizer is used)
  # ----------------------------------------------------------------------

    include "FprimeSoakTestReferenceDeploymentPackets.fppi"

  # ----------------------------------------------------------------------
  # Direct graph specifiers
  # ----------------------------------------------------------------------

    connections ComCcsds_CdhCore {
      # Core events and telemetry to communication queue
      CdhCore.events.PktSend -> ComCcsds.comQueue.comPacketQueueIn[ComCcsds.Ports_ComPacketQueue.EVENTS]
      CdhCore.tlmSend.PktSend -> ComCcsds.comQueue.comPacketQueueIn[ComCcsds.Ports_ComPacketQueue.TELEMETRY]

      # Router to Command Dispatcher
      ComCcsds.fprimeRouter.commandOut -> CdhCore.cmdDisp.seqCmdBuff
      CdhCore.cmdDisp.seqCmdStatus -> ComCcsds.fprimeRouter.cmdResponseIn
    }

    connections ComCcsds_FileHandling {
      # File Downlink to Communication Queue
      FileHandling.fileDownlink.bufferSendOut -> ComCcsds.comQueue.bufferQueueIn[ComCcsds.Ports_ComBufferQueue.FILE]
      ComCcsds.comQueue.bufferReturnOut[ComCcsds.Ports_ComBufferQueue.FILE] -> FileHandling.fileDownlink.bufferReturn

      # Router to File Uplink
      ComCcsds.fprimeRouter.fileOut -> FileHandling.fileUplink.bufferSendIn
      FileHandling.fileUplink.bufferSendOut -> ComCcsds.fprimeRouter.fileBufferReturnIn
    }

    connections Communications {
      # RFM69 manager buffer allocations
      Rfm69.rfm69Manager.allocate   -> ComCcsds.commsBufferManager.bufferGetCallee
      Rfm69.rfm69Manager.deallocate -> ComCcsds.commsBufferManager.bufferSendIn

      # Aggregated space packets -> ComRetry -> RFM69 (Downlink). Rfm69Manager
      # rejects frames during RX/TX holdoff or TX busy; ComRetry resends them.
      ComCcsds.SpacePacketFraming.dataOut       -> comRetry.dataIn
      comRetry.dataReturnOut                    -> ComCcsds.SpacePacketFraming.dataReturnIn
      comRetry.comStatusOut                     -> ComCcsds.SpacePacketFraming.comStatusIn
      comRetry.dataOut                          -> Rfm69.rfm69Manager.dataIn
      Rfm69.rfm69Manager.dataReturnOut          -> comRetry.dataReturnIn
      Rfm69.rfm69Manager.comStatusOut           -> comRetry.comStatusIn

      # RFM69 <-> SpacePacketDeframer (Uplink; one complete SP per RF packet)
      Rfm69.rfm69Manager.dataOut                -> ComCcsds.SpacePacketFraming.dataIn
      ComCcsds.SpacePacketFraming.dataReturnOut -> Rfm69.rfm69Manager.dataReturnIn
    }

    connections FileHandling_DataProducts {
      # Data Products to File Downlink
      DataProducts.dpCat.fileOut -> FileHandling.fileDownlink.SendFile
      FileHandling.fileDownlink.FileComplete -> DataProducts.dpCat.fileDone
    }

    connections RateGroups {
      # timer to drive rate group
      timer.CycleOut -> rateGroupDriver.CycleIn

      # Rate group 1KHz: Command sequencer + RFM69 RX poll
      rateGroupDriver.CycleOut[Ports_RateGroups.rateGroup1KHz] -> rateGroup1KHz.CycleIn
      rateGroup1KHz.RateGroupMemberOut[0] -> cmdSeq.schedIn
      rateGroup1KHz.RateGroupMemberOut[1] -> Rfm69.rfm69Manager.run

      # Rate group 10Hz: Sensors, file downlink
      rateGroupDriver.CycleOut[Ports_RateGroups.rateGroup10Hz] -> rateGroup10Hz.CycleIn
      rateGroup10Hz.RateGroupMemberOut[0] -> Bmp280.bmpManager.run
      rateGroup10Hz.RateGroupMemberOut[1] -> MpuImu.imuManager.run
      rateGroup10Hz.RateGroupMemberOut[2] -> FileHandling.fileDownlink.Run

      # Rate group 1Hz: Housekeeping, ComQueue, telemetry, and aggregator flush.
      # Aggregator timeout at 1 Hz lets space packets fill toward the RF MTU
      # before TX (fewer half-empty packets at 19.2 kbps).
      rateGroupDriver.CycleOut[Ports_RateGroups.rateGroup1Hz] -> rateGroup1Hz.CycleIn
      rateGroup1Hz.RateGroupMemberOut[0] -> CdhCore.$health.Run
      rateGroup1Hz.RateGroupMemberOut[1] -> systemResources.run
      rateGroup1Hz.RateGroupMemberOut[2] -> ComCcsds.commsBufferManager.schedIn
      rateGroup1Hz.RateGroupMemberOut[3] -> DataProducts.dpBufferManager.schedIn
      rateGroup1Hz.RateGroupMemberOut[4] -> DataProducts.dpWriter.schedIn
      rateGroup1Hz.RateGroupMemberOut[5] -> DataProducts.dpMgr.schedIn
      rateGroup1Hz.RateGroupMemberOut[6] -> ComCcsds.comQueue.run
      rateGroup1Hz.RateGroupMemberOut[7] -> CdhCore.tlmSend.Run
      rateGroup1Hz.RateGroupMemberOut[8] -> ComCcsds.aggregator.timeout
    }

    connections CdhCore_cmdSeq {
      # Command Sequencer
      cmdSeq.comCmdOut -> CdhCore.cmdDisp.seqCmdBuff
      CdhCore.cmdDisp.seqCmdStatus -> cmdSeq.cmdResponseIn
    }

    connections FprimeSoakTestReferenceDeployment {
       # Sensor managers push readings into the application component
       Bmp280.bmpManager.bmpDataPush -> sensorDataProducer.bmpDataIn
       MpuImu.imuManager.imuDataPush -> sensorDataProducer.imuDataIn

       # Application component produces data products
       sensorDataProducer.productGetOut  -> DataProducts.Subtopology.productGetIn
       sensorDataProducer.productSendOut -> DataProducts.Subtopology.productSendIn
    }

  }

}
