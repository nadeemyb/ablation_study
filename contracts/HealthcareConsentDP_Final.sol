// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title HealthcareConsentDP_Final
 * @notice Single-contract implementation combining:
 *
 *  1. Dynamic Consent Management (DCMS) — the Data Subject (Patient)
 *     sets/updates/revokes consent (purpose, access level, validity
 *     period, data sensitivity), recorded immutably on-chain.
 *
 *  2. Policy-Based Access Control (PBAC) — the Data Controller
 *     registers patients and requesters (with a role), and every
 *     access request from a Data Requester is evaluated against the
 *     patient's active consent policy before anything is released.
 *
 *  3. Consent-aware Differential Privacy budgeting — once (and only
 *     once) a request is Granted/PartialGranted, a patient-specific
 *     privacy parameter (epsilon) is derived on-chain from the same
 *     consent attributes, and an explicit per-patient epsilon budget is
 *     checked/deducted against it.
 *
 * IMPORTANT — split responsibility with off-chain Python:
 * The actual noise generation (Laplace, Gaussian, or Exponential
 * mechanism) is NOT performed on-chain in this version. Solidity has
 * no native floating-point support, so earlier fixed-point/Taylor-
 * approximation noise math is error-prone and imprecise. Instead:
 *   - This contract derives epsilon (deriveEpsilon) and enforces the
 *     epsilon budget on-chain, so those trust-critical decisions
 *     remain tamper-resistant and auditable.
 *   - A Python script (e.g. using NumPy/SciPy) reads epsilon via
 *     deriveEpsilon() or the DPReleased event, computes the true
 *     Laplace/Gaussian/Exponential noise with correct floating-point
 *     arithmetic, and submits the already-perturbed value back via
 *     releasePrivateAggregate(). The contract re-derives epsilon
 *     itself rather than trusting a caller-supplied value, so budget
 *     deduction cannot be spoofed by the off-chain script.
 *   - Raw patient records are never exposed here — only IPFS
 *     CIDs/hashes (for the encrypted source record) and, optionally,
 *     a CID for the perturbed output.
 *
 * Kept in a single contract (single ABI, single deployed address) to
 * simplify off-chain benchmarking scripts (e.g. Python + web3.py).
 */
contract HealthcareConsentDP_Final {

    // ------------------------------------------------------------
    // Roles / actors
    // ------------------------------------------------------------

    address public controller;

    modifier onlyController() {
        require(msg.sender == controller, "Only controller");
        _;
    }

    // ------------------------------------------------------------
    // Enumerations
    // ------------------------------------------------------------

    enum AccessLevel     { None, Minor, Partial, Full }
    enum DataSensitivity { Low, Medium, High }
    enum Purpose         { Research, Analysis, Treatment, Both, Commercial }
    enum ConsentStatus   { Pending, Granted, PartialGranted, Denied }
    enum RequesterRole   { Unregistered, Clinician, Researcher, PublicHealthAnalyst, CommercialPartner }

    enum DenialReason {
        None,
        PatientDoesNotExist,
        NoActivePolicy,
        PolicyInactive,
        PolicyRevoked,
        PolicyExpired,
        PurposeMismatch,
        AccessLevelTooHigh,
        ConsentNotGranted,
        RequesterNotRegistered,
        RoleNotPermittedForPurpose
    }

    // ------------------------------------------------------------
    // Data structures
    // ------------------------------------------------------------

    struct PatientRecord {
        uint256 patientId;
        address patientAddress;
        bool exists;

        DataSensitivity sensitivity;

        // Off-chain (IPFS) references to the encrypted record, plus
        // on-chain hashes for integrity verification.
        string partialCid;
        bytes32 partialHash;

        string fullCid;
        bytes32 fullHash;

        uint256 activePolicyId;
    }

    struct ConsentPolicy {
        uint256 policyId;
        uint256 patientId;

        Purpose purpose;
        AccessLevel access;

        bool isActive;
        bool consent;
        bool revoked;

        address creator;

        uint256 validFrom;
        uint256 validUntil;
    }

    struct RequesterProfile {
        RequesterRole role;
        bool isRegistered;
        string organization;
    }

    struct AccessRequest {
        uint256 requestId;
        address requester;
        uint256 patientId;

        Purpose purpose;
        AccessLevel requestedAccess;

        ConsentStatus status;
        DenialReason reason;

        uint256 policyIdAtRequest;
        uint256 timestamp;
    }

    // Which DP mechanism was applied by the trusted off-chain privacy engine
    // to produce this release's perturbed value.
    enum Mechanism { Laplace, Gaussian, Exponential }

    struct DPRelease {
        uint256 requestId;
        uint256 patientId;
        int256 releasedValue; // fixed-point DP-protected value only
        uint256 epsilonUsed;
        uint256 querySensitivity;
        Mechanism mechanism;
        bytes32 releaseHash;  // hash of the DP-protected IPFS payload
        string perturbedCid;
        uint256 timestamp;
    }

    // ------------------------------------------------------------
    // Storage
    // ------------------------------------------------------------

    uint256 public lastPolicyId;

    mapping(uint256 => PatientRecord) public patientRecords;
    mapping(uint256 => ConsentPolicy) public policies;
    mapping(uint256 => AccessRequest) public accessRequests;
    mapping(address => RequesterProfile) public requesterProfiles;

    mapping(RequesterRole => mapping(Purpose => bool)) public allowedPurposeForRole;

    // Per-patient epsilon budget (scaled like epsilon, see below).
    // Remaining cumulative epsilon budget for each patient (SCALE-fixed-point units).
    mapping(uint256 => uint256) public epsilonBudget;
    mapping(uint256 => bool) public budgetInitialized;

    mapping(uint256 => DPRelease[]) public releasesByRequest;

    // ------------------------------------------------------------
    // Fixed-point DP configuration (SCALE = 1.000000)
    // ------------------------------------------------------------

    uint256 constant SCALE = 1_000_000;

    // Operational epsilon range used by the framework: [0.10, 1.00]
    uint256 constant EPS_MIN = 100_000;   // 0.10
    uint256 constant EPS_MAX = 1_000_000; // 1.00

    // Feasible bounds of the combined geometric consent score,
    // computed once in the constructor.
    uint256 public immutable G_MIN;
    uint256 public immutable G_MAX;

    // Epsilon budget is independent of the first query's epsilon. This avoids
    // the circular "first query chooses the total budget" behavior. Values are
    // SCALE-fixed-point epsilon units. Patients/controllers may reduce the
    // remaining budget at any time, but may not increase it after a release.
    uint256 public constant DEFAULT_TOTAL_EPSILON_BUDGET = 5_000_000; // 5.0
    uint256 public constant MAX_TOTAL_EPSILON_BUDGET = 10_000_000;    // 10.0
    mapping(uint256 => uint256) public patientReleaseCount;
    mapping(uint256 => bool) public requestReleased;

    // ------------------------------------------------------------
    // Events (audit log)
    // ------------------------------------------------------------

    event PatientRecordRegistered(
        uint256 indexed patientId,
        address indexed patientAddress,
        DataSensitivity sensitivity,
        string partialCid,
        string fullCid
    );

    event PolicyCreated(
        uint256 indexed policyId,
        uint256 indexed patientId,
        Purpose purpose,
        AccessLevel access,
        bool consent,
        uint256 validFrom,
        uint256 validUntil
    );

    event ConsentUpdated(uint256 indexed policyId, bool consent);
    event PolicyRevoked(uint256 indexed policyId);

    event RequesterRegistered(
        address indexed requester,
        RequesterRole role,
        string organization
    );

    event AccessRequested(
        uint256 indexed requestId,
        uint256 indexed patientId,
        address indexed requester,
        Purpose purpose,
        AccessLevel requestedAccess
    );

    event PartialAccessGranted(uint256 indexed requestId, string cid, bytes32 recordHash);
    event AccessGranted(uint256 indexed requestId, string cid, bytes32 recordHash);
    event AccessDenied(uint256 indexed requestId, DenialReason reason);

    event EpsilonBudgetInitialized(uint256 indexed patientId, uint256 initialBudget);
    event EpsilonBudgetUpdated(uint256 indexed patientId, uint256 previousBudget, uint256 newBudget);
    event DPReleased(
        uint256 indexed requestId,
        uint256 indexed patientId,
        int256 releasedValue,
        uint256 epsilonUsed,
        uint256 querySensitivity,
        Mechanism mechanism,
        bytes32 releaseHash,
        string perturbedCid
    );

    // ------------------------------------------------------------
    // Constructor
    // ------------------------------------------------------------

    constructor() {
        controller = msg.sender;

        // Default role -> permitted purpose matrix.
        allowedPurposeForRole[RequesterRole.Clinician][Purpose.Treatment] = true;
        allowedPurposeForRole[RequesterRole.Clinician][Purpose.Both] = true;

        allowedPurposeForRole[RequesterRole.Researcher][Purpose.Research] = true;
        allowedPurposeForRole[RequesterRole.Researcher][Purpose.Analysis] = true;
        allowedPurposeForRole[RequesterRole.Researcher][Purpose.Both] = true;

        allowedPurposeForRole[RequesterRole.PublicHealthAnalyst][Purpose.Analysis] = true;
        allowedPurposeForRole[RequesterRole.CommercialPartner][Purpose.Commercial] = true;

        // Score bounds, scaled by SCALE, matching the attribute tables
        // used for epsilon derivation below:
        //   Purpose:     min 200000 (Research)   max 1000000 (Commercial)
        //   AccessLevel: min 200000 (Minor)       max 1000000 (Full)
        //   Validity:    min 100000 (<6 months)   max 1000000 (>12 months)
        //   Sensitivity: min 100000 (High)        max 1000000 (Low)
        G_MIN = geometricMean4(200_000, 200_000, 100_000, 100_000);
        G_MAX = geometricMean4(1_000_000, 1_000_000, 1_000_000, 1_000_000);
    }

    // ------------------------------------------------------------
    // 1. Patient record registration (Controller only)
    // ------------------------------------------------------------

    function registerPatientRecord(
        uint256 patientId,
        address patientAddress,
        DataSensitivity sensitivity,
        string memory partialCid,
        bytes32 partialHash,
        string memory fullCid,
        bytes32 fullHash
    )
        external
        onlyController
    {
        require(patientId != 0, "Invalid patientId");
        require(patientAddress != address(0), "Invalid patient address");
        require(!patientRecords[patientId].exists, "Patient already exists");
        require(bytes(partialCid).length > 0, "Partial CID required");
        require(bytes(fullCid).length > 0, "Full CID required");

        patientRecords[patientId] = PatientRecord({
            patientId: patientId,
            patientAddress: patientAddress,
            exists: true,
            sensitivity: sensitivity,
            partialCid: partialCid,
            partialHash: partialHash,
            fullCid: fullCid,
            fullHash: fullHash,
            activePolicyId: 0
        });

        epsilonBudget[patientId] = DEFAULT_TOTAL_EPSILON_BUDGET;
        budgetInitialized[patientId] = true;

        emit PatientRecordRegistered(patientId, patientAddress, sensitivity, partialCid, fullCid);
        emit EpsilonBudgetInitialized(patientId, DEFAULT_TOTAL_EPSILON_BUDGET);
    }

    // ------------------------------------------------------------
    // 2. Consent-policy creation (Patient or Controller)
    // ------------------------------------------------------------

    function createPolicy(
        uint256 patientId,
        Purpose purpose,
        AccessLevel access,
        uint256 durationSeconds,
        bool initialConsent
    )
        external
        returns (uint256)
    {
        PatientRecord storage patient = patientRecords[patientId];
        require(patient.exists, "Patient does not exist");
        require(
            msg.sender == patient.patientAddress || msg.sender == controller,
            "Not authorized"
        );
        require(access != AccessLevel.None, "Invalid access level");
        require(durationSeconds > 0, "Duration required");

        uint256 policyId = ++lastPolicyId;
        uint256 startTime = block.timestamp;
        uint256 endTime = startTime + durationSeconds;

        policies[policyId] = ConsentPolicy({
            policyId: policyId,
            patientId: patientId,
            purpose: purpose,
            access: access,
            isActive: true,
            consent: initialConsent,
            revoked: false,
            creator: msg.sender,
            validFrom: startTime,
            validUntil: endTime
        });

        patient.activePolicyId = policyId;

        emit PolicyCreated(policyId, patientId, purpose, access, initialConsent, startTime, endTime);
        return policyId;
    }

    // ------------------------------------------------------------
    // 3. Consent update
    // ------------------------------------------------------------

    function updateConsent(uint256 policyId, bool newConsent) external {
        ConsentPolicy storage policy = policies[policyId];
        require(policy.policyId != 0, "Policy does not exist");
        require(_canManagePolicy(policy), "Not authorized");
        require(!policy.revoked, "Policy revoked");

        policy.consent = newConsent;
        emit ConsentUpdated(policyId, newConsent);
    }

    // ------------------------------------------------------------
    // 4. Policy revocation
    // ------------------------------------------------------------

    function revokePolicy(uint256 policyId) external {
        ConsentPolicy storage policy = policies[policyId];
        require(policy.policyId != 0, "Policy does not exist");
        require(_canManagePolicy(policy), "Not authorized");

        policy.revoked = true;
        policy.isActive = false;
        policy.consent = false;

        emit PolicyRevoked(policyId);
    }

    // ------------------------------------------------------------
    // 5. Requester registration (role-based access control) —
    //    Controller only, i.e. "checks requester legitimacy"
    // ------------------------------------------------------------

    function registerRequester(
        address requesterAddress,
        RequesterRole role,
        string memory organization
    )
        external
        onlyController
    {
        require(requesterAddress != address(0), "Invalid requester address");
        require(role != RequesterRole.Unregistered, "Invalid role");

        requesterProfiles[requesterAddress] = RequesterProfile({
            role: role,
            isRegistered: true,
            organization: organization
        });

        emit RequesterRegistered(requesterAddress, role, organization);
    }

    // ------------------------------------------------------------
    // 6. Submit PBAC access request
    // ------------------------------------------------------------

    function submitAccessRequest(
        uint256 requestId,
        uint256 patientId,
        Purpose purpose,
        AccessLevel requestedAccess
    )
        external
    {
        require(requestId != 0, "Invalid requestId");
        require(accessRequests[requestId].timestamp == 0, "Request already exists");
        require(requesterProfiles[msg.sender].isRegistered, "Requester not registered");
        require(requestedAccess != AccessLevel.None, "Invalid access request");

        PatientRecord storage patient = patientRecords[patientId];
        require(patient.exists, "Patient does not exist");
        require(patient.activePolicyId != 0, "No active policy");

        accessRequests[requestId] = AccessRequest({
            requestId: requestId,
            requester: msg.sender,
            patientId: patientId,
            purpose: purpose,
            requestedAccess: requestedAccess,
            status: ConsentStatus.Pending,
            reason: DenialReason.None,
            policyIdAtRequest: patient.activePolicyId,
            timestamp: block.timestamp
        });

        emit AccessRequested(requestId, patientId, msg.sender, purpose, requestedAccess);
    }

    // ------------------------------------------------------------
    // 7. Evaluate PBAC request (role + consent + policy checks)
    // ------------------------------------------------------------

    function evaluateRequest(uint256 requestId) public onlyController {
        AccessRequest storage requestData = accessRequests[requestId];
        require(requestData.timestamp != 0, "Request does not exist");

        PatientRecord storage patient = patientRecords[requestData.patientId];

        if (!patient.exists) {
            _deny(requestData, DenialReason.PatientDoesNotExist);
            return;
        }

        RequesterProfile storage rp = requesterProfiles[requestData.requester];
        if (!rp.isRegistered) {
            _deny(requestData, DenialReason.RequesterNotRegistered);
            return;
        }

        if (!allowedPurposeForRole[rp.role][requestData.purpose]) {
            _deny(requestData, DenialReason.RoleNotPermittedForPurpose);
            return;
        }

        ConsentPolicy storage policy = policies[requestData.policyIdAtRequest];
        DenialReason result = _checkPolicyAgainstRequest(requestData, policy);

        if (result != DenialReason.None) {
            _deny(requestData, result);
            return;
        }

        if (!policy.consent) {
            _deny(requestData, DenialReason.ConsentNotGranted);
            return;
        }

        if (
            policy.access == AccessLevel.Partial ||
            requestData.requestedAccess == AccessLevel.Partial
        ) {
            requestData.status = ConsentStatus.PartialGranted;
            requestData.reason = DenialReason.None;
            emit PartialAccessGranted(requestId, patient.partialCid, patient.partialHash);
        } else {
            requestData.status = ConsentStatus.Granted;
            requestData.reason = DenialReason.None;
            emit AccessGranted(requestId, patient.fullCid, patient.fullHash);
        }
    }

    function _deny(AccessRequest storage requestData, DenialReason reason) internal {
        requestData.status = ConsentStatus.Denied;
        requestData.reason = reason;
        emit AccessDenied(requestData.requestId, reason);
    }

    // ------------------------------------------------------------
    // 8. Requester retrieves its own decision + data reference
    // ------------------------------------------------------------

    function statusByRequester(uint256 requestId)
        external
        view
        returns (
            ConsentStatus status,
            DenialReason reason,
            uint256 policyId,
            string memory resultCid,
            bytes32 resultHash
        )
    {
        AccessRequest storage requestData = accessRequests[requestId];
        require(requestData.timestamp != 0, "Request does not exist");
        require(requestData.requester == msg.sender, "Only requester can check");

        PatientRecord storage patient = patientRecords[requestData.patientId];

        status = requestData.status;
        reason = requestData.reason;
        policyId = requestData.policyIdAtRequest;

        if (requestData.status == ConsentStatus.PartialGranted) {
            resultCid = patient.partialCid;
            resultHash = patient.partialHash;
        } else if (requestData.status == ConsentStatus.Granted) {
            resultCid = patient.fullCid;
            resultHash = patient.fullHash;
        } else {
            resultCid = "";
            resultHash = bytes32(0);
        }
    }

    // ------------------------------------------------------------
    // Internal PBAC policy checking
    // ------------------------------------------------------------

    function _checkPolicyAgainstRequest(
        AccessRequest memory requestData,
        ConsentPolicy memory policy
    )
        internal
        view
        returns (DenialReason)
    {
        if (policy.policyId == 0) return DenialReason.NoActivePolicy;
        if (policy.revoked) return DenialReason.PolicyRevoked;
        if (!policy.isActive) return DenialReason.PolicyInactive;
        if (block.timestamp > policy.validUntil) return DenialReason.PolicyExpired;

        if (requestData.purpose != policy.purpose) {
            return DenialReason.PurposeMismatch;
        }

        // None=0, Minor=1, Partial=2, Full=3
        if (uint8(requestData.requestedAccess) > uint8(policy.access)) {
            return DenialReason.AccessLevelTooHigh;
        }

        return DenialReason.None;
    }

    function _canManagePolicy(ConsentPolicy memory policy) internal view returns (bool) {
        PatientRecord storage patient = patientRecords[policy.patientId];

        if (msg.sender == controller) return true;
        if (msg.sender == patient.patientAddress) return true;
        if (msg.sender == policy.creator) return true;

        return false;
    }

    // ==============================================================
    // DIFFERENTIAL PRIVACY ENGINE
    // (only reachable after PBAC has Granted/PartialGranted a request)
    // ==============================================================

    // ---- Attribute -> normalized score mapping (scaled by SCALE) ----

    // Purpose: 0=Research,1=Analysis,2=Treatment,3=Both,4=Commercial
    function purposeScore(uint8 purpose) internal pure returns (uint256) {
        if (purpose == 0) return 200_000; // Research
        if (purpose == 1) return 500_000; // Analysis
        if (purpose == 3) return 650_000; // Both
        if (purpose == 2) return 800_000; // Treatment
        return 1_000_000;                 // Commercial
    }

    // AccessLevel: 0=None,1=Minor,2=Partial,3=Full
    function accessScore(uint8 accessLevel) internal pure returns (uint256) {
        if (accessLevel == 0) return 0;
        if (accessLevel == 1) return 200_000;
        if (accessLevel == 2) return 500_000;
        return 1_000_000;
    }

    // Remaining consent validity relative to now.
    function validityScore(uint256 validUntil) internal view returns (uint256) {
        if (block.timestamp >= validUntil) return 0;
        uint256 remaining = validUntil - block.timestamp;
        if (remaining < 180 days) return 100_000;
        if (remaining < 365 days) return 500_000;
        return 1_000_000;
    }

    // DataSensitivity: 0=Low,1=Medium,2=High
    // Higher sensitivity -> lower score -> smaller resulting epsilon.
    function sensitivityScore(uint8 sensitivity) internal pure returns (uint256) {
        if (sensitivity == 2) return 100_000; // High
        if (sensitivity == 1) return 500_000; // Medium
        return 1_000_000;                     // Low
    }

    // ---- Fixed-point math helpers ----

    // Babylonian method integer square root.
    function sqrt(uint256 x) internal pure returns (uint256 y) {
        if (x == 0) return 0;
        uint256 z = (x + 1) / 2;
        y = x;
        while (z < y) {
            y = z;
            z = (x / z + z) / 2;
        }
    }

    // Geometric mean of four SCALE-fixed-point values:
    // (a*b*c*d)^(1/4) computed as sqrt(sqrt(a*b*c*d)).
    function geometricMean4(uint256 a, uint256 b, uint256 c, uint256 d)
        internal
        pure
        returns (uint256)
    {
        uint256 product = a * b * c * d; // max (1e6)^4 = 1e24, fits uint256
        return sqrt(sqrt(product));
    }

    // ---- Dynamic epsilon derivation (patient-specific, consent-driven) ----

    function _deriveEpsilonForPolicy(uint256 patientId, uint256 policyId)
        internal
        view
        returns (uint256 epsilon)
    {
        PatientRecord storage patient = patientRecords[patientId];
        require(patient.exists, "Patient does not exist");
        require(policyId != 0, "No policy");

        ConsentPolicy storage policy = policies[policyId];
        require(policy.patientId == patientId, "Policy/patient mismatch");

        uint256 p = purposeScore(uint8(policy.purpose));
        uint256 a = accessScore(uint8(policy.access));
        require(a > 0, "Access level None - not authorized for DP release");
        uint256 t = validityScore(policy.validUntil);
        uint256 s = sensitivityScore(uint8(patient.sensitivity));

        uint256 g = geometricMean4(p, a, t, s);
        uint256 gClamped = g < G_MIN ? G_MIN : (g > G_MAX ? G_MAX : g);
        uint256 normalized = ((gClamped - G_MIN) * SCALE) / (G_MAX - G_MIN);
        epsilon = EPS_MIN + ((EPS_MAX - EPS_MIN) * normalized) / SCALE;
    }

    function deriveEpsilon(uint256 patientId) public view returns (uint256 epsilon) {
        PatientRecord storage patient = patientRecords[patientId];
        require(patient.exists, "Patient does not exist");
        require(patient.activePolicyId != 0, "No active policy");
        return _deriveEpsilonForPolicy(patientId, patient.activePolicyId);
    }

    function deriveEpsilonForRequest(uint256 requestId)
        public
        view
        returns (uint256 epsilon)
    {
        AccessRequest storage requestData = accessRequests[requestId];
        require(requestData.timestamp != 0, "Request does not exist");
        return _deriveEpsilonForPolicy(
            requestData.patientId,
            requestData.policyIdAtRequest
        );
    }

    // The statistically correct DP mechanism runs in the trusted Python
    // privacy engine. Only the already-perturbed value is committed on-chain.
    // Raw noise and the true query result are deliberately NOT stored because
    // publishing either together with the noisy output would undermine privacy.
    function releasePrivateAggregate(
        uint256 requestId,
        int256 releasedValue,
        uint256 querySensitivity,
        Mechanism mechanism,
        bytes32 releaseHash,
        string calldata perturbedCid
    )
        external
        returns (uint256 epsilonUsed)
    {
        AccessRequest storage requestData = accessRequests[requestId];
        require(requestData.timestamp != 0, "Request does not exist");
        require(
            requestData.status == ConsentStatus.Granted ||
            requestData.status == ConsentStatus.PartialGranted,
            "Request not authorized by PBAC"
        );
        require(
            msg.sender == requestData.requester || msg.sender == controller,
            "Only authorized requester/controller can release"
        );
        require(querySensitivity > 0, "Invalid sensitivity");
        require(releaseHash != bytes32(0), "Missing DP release hash");

        uint256 patientId = requestData.patientId;
        ConsentPolicy storage policy = policies[requestData.policyIdAtRequest];

        // Revalidate consent at release time so a grant cannot be replayed
        // after revocation or expiry.
        require(policy.isActive, "Policy inactive at release");
        require(!policy.revoked, "Policy revoked at release");
        require(policy.consent, "Consent withdrawn at release");
        require(block.timestamp <= policy.validUntil, "Policy expired at release");

        require(!requestReleased[requestId], "DP release already committed for request");

        epsilonUsed = _deriveEpsilonForPolicy(
            patientId,
            requestData.policyIdAtRequest
        );

        require(budgetInitialized[patientId], "Epsilon budget not initialized");
        require(
            epsilonBudget[patientId] >= epsilonUsed,
            "Epsilon budget exhausted for this patient"
        );

        epsilonBudget[patientId] -= epsilonUsed;
        requestReleased[requestId] = true;
        patientReleaseCount[patientId] += 1;

        releasesByRequest[requestId].push(DPRelease({
            requestId: requestId,
            patientId: patientId,
            releasedValue: releasedValue,
            epsilonUsed: epsilonUsed,
            querySensitivity: querySensitivity,
            mechanism: mechanism,
            releaseHash: releaseHash,
            perturbedCid: perturbedCid,
            timestamp: block.timestamp
        }));

        emit DPReleased(
            requestId,
            patientId,
            releasedValue,
            epsilonUsed,
            querySensitivity,
            mechanism,
            releaseHash,
            perturbedCid
        );
    }

    // Patient/controller epsilon-budget control. Before the first release the
    // budget may be set within the declared cap; after a release it may only be
    // reduced, preventing post-hoc budget top-ups that weaken composition.
    function setEpsilonBudget(uint256 patientId, uint256 newBudget) external {
        PatientRecord storage patient = patientRecords[patientId];
        require(patient.exists, "Patient does not exist");
        require(
            msg.sender == patient.patientAddress || msg.sender == controller,
            "Not authorized"
        );
        require(newBudget <= MAX_TOTAL_EPSILON_BUDGET, "Budget exceeds cap");

        uint256 previous = epsilonBudget[patientId];
        if (patientReleaseCount[patientId] > 0) {
            require(newBudget <= previous, "Cannot increase budget after release");
        }
        epsilonBudget[patientId] = newBudget;
        budgetInitialized[patientId] = true;
        emit EpsilonBudgetUpdated(patientId, previous, newBudget);
    }

    function getRequesterProfile(address requester)
        external
        view
        returns (RequesterRole role, bool isRegistered, string memory organization)
    {
        RequesterProfile storage p = requesterProfiles[requester];
        return (p.role, p.isRegistered, p.organization);
    }

    function getAccessRequest(uint256 requestId)
        external
        view
        returns (
            address requester,
            uint256 patientId,
            Purpose purpose,
            AccessLevel requestedAccess,
            ConsentStatus status,
            DenialReason reason,
            uint256 policyIdAtRequest,
            uint256 timestamp
        )
    {
        AccessRequest storage r = accessRequests[requestId];
        return (
            r.requester, r.patientId, r.purpose, r.requestedAccess,
            r.status, r.reason, r.policyIdAtRequest, r.timestamp
        );
    }

    // ---- View helpers ----

    function getRemainingEpsilonBudget(uint256 patientId) external view returns (uint256) {
        return epsilonBudget[patientId];
    }

    function getReleaseCount(uint256 requestId) external view returns (uint256) {
        return releasesByRequest[requestId].length;
    }

    function getRelease(uint256 requestId, uint256 index)
        external
        view
        returns (
            uint256 patientId,
            int256 releasedValue,
            uint256 epsilonUsed,
            uint256 querySensitivity,
            Mechanism mechanism,
            bytes32 releaseHash,
            string memory perturbedCid,
            uint256 timestamp
        )
    {
        DPRelease memory r = releasesByRequest[requestId][index];
        return (
            r.patientId,
            r.releasedValue,
            r.epsilonUsed,
            r.querySensitivity,
            r.mechanism,
            r.releaseHash,
            r.perturbedCid,
            r.timestamp
        );
    }
}
