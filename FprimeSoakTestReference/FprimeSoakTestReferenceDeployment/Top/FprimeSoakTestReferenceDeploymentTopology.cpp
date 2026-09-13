// ======================================================================
// \title  FprimeSoakTestReferenceDeploymentTopology.cpp
// \brief cpp file containing the topology instantiation code
//
// ======================================================================
// Provides access to autocoded functions
#include <FprimeSoakTestReference/FprimeSoakTestReferenceDeployment/Top/FprimeSoakTestReferenceDeploymentTopologyAc.hpp>
// Include telemetry packet definitions for TlmPacketizer
#include <FprimeSoakTestReference/FprimeSoakTestReferenceDeployment/Top/FprimeSoakTestReferenceDeployment_FprimeSoakTestReferenceDeploymentPacketsTlmPacketsAc.hpp>

// Necessary project-specified types
#include <Fw/Types/MallocAllocator.hpp>

// Public functions for use in main program are namespaced with deployment module FprimeSoakTestReference
// This is also the namespace where the topology components are instantiated by FPP.
namespace FprimeSoakTestReference {

// Instantiate a malloc allocator for cmdSeq buffer allocation
Fw::MallocAllocator mallocator;

// The topology divides the incoming clock signal (1KHz) into sub-signals with 0 offset:
//   rateGroup1KHz = 1000/1    =  1KHz (1ms)   - command sequencer, RFM69 run (RX poll)
//   rateGroup10Hz = 1000/100  =  10Hz (100ms) - sensors, file downlink
//   rateGroup1Hz  = 1000/1000 =  1Hz (1s)     - health / DP / ComQueue / tlmSend / aggregator
Svc::RateGroupDriver::DividerSet rateGroupDivisorsSet{{{1, 0}, {100, 0}, {1000, 0}}};

// Rate groups may supply a context token to each of the attached children whose purpose is set by the project. The
// reference topology sets each token to zero as these contexts are unused in this project.
Svc::ActiveRateGroup::ContextArray rateGroup1KHzContext(0);
Svc::ActiveRateGroup::ContextArray rateGroup10HzContext(0);
Svc::ActiveRateGroup::ContextArray rateGroup1HzContext(0);

// Rfm69Manager rejects a frame while in RX/TX holdoff or TX busy; each rejection is
// followed by a resume, so a few retries suffice even under paced file uplink.
static const U32 COM_RETRY_MAX_RETRIES = 10;

/**
 * \brief configure/setup components in project-specific way
 *
 * This is a *helper* function which configures/sets up each component requiring project specific input. This includes
 * allocating resources, passing-in arguments, etc. This function may be inlined into the topology setup function if
 * desired, but is extracted here for clarity.
 */
void configureTopology() {
    // Rate group driver needs a divisor list
    rateGroupDriver.configure(rateGroupDivisorsSet);

    // Rate groups require context arrays.
    rateGroup1KHz.configure(rateGroup1KHzContext);
    rateGroup10Hz.configure(rateGroup10HzContext);
    rateGroup1Hz.configure(rateGroup1HzContext);

    // Command sequencer needs to allocate memory to hold contents of command sequences
    cmdSeq.allocateBuffer(0, mallocator, 5 * 1024);

    comRetry.configure(COM_RETRY_MAX_RETRIES);

    // PrmDb file name must be supplied by the using topology (required for PRM_SAVE_FILE)
    FileHandling::prmDb.configure("/home/pi/fprime/PrmDb.dat");
}

void setupTopology(const TopologyState& state) {
    // Autocoded initialization. Function provided by autocoder.
    initComponents(state);
    // Autocoded id setup. Function provided by autocoder.
    setBaseIds();
    // Autocoded connection wiring. Function provided by autocoder.
    connectComponents();
    // Autocoded command registration. Function provided by autocoder.
    regCommands();
    // Autocoded configuration. Function provided by autocoder.
    configComponents(state);
    // Project-specific component configuration. Function provided above. May be inlined, if desired.
    // Must run before readParameters() so prmDb has a configured file path.
    configureTopology();
    // Autocoded: load PrmDb.dat into FileHandling::prmDb (from FileHandling.fpp phase).
    readParameters();
    // Autocoded: each component requests its parameters from prmDb.
    loadParameters();
    // Autocoded task kick-off (active components). Function provided by autocoder.
    startTasks(state);
}

void startRateGroups(const Fw::TimeInterval& interval) {
    // The timer component drives the fundamental tick rate of the system.
    // Svc::RateGroupDriver will divide this down to the slower rate groups.
    // This call will block until the stopRateGroups() call is made.
    timer.startTimer(interval);
}

void stopRateGroups() {
    timer.quit();
}

void teardownTopology(const TopologyState& state) {
    // Autocoded (active component) task clean-up. Functions provided by topology autocoder.
    stopTasks(state);
    freeThreads(state);

    // Resource deallocation
    cmdSeq.deallocateBuffer(mallocator);

    tearDownComponents(state);
    deinitComponents(state);
}
};  // namespace FprimeSoakTestReference
