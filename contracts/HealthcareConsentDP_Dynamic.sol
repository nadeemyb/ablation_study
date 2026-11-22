// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;
import "./HealthcareConsentDP_Final.sol";

/// Versioned experimental extension; original source/artifact remain unchanged.
contract HealthcareConsentDP_Dynamic is HealthcareConsentDP_Final {
    event ConsentParametersUpdated(uint256 indexed patientId, uint256 oldPolicyId,
        uint256 newPolicyId);

    function updateConsentParameters(uint256 patientId, Purpose purpose,
        AccessLevel access, uint256 durationSeconds, bool consent,
        DataSensitivity sensitivity) external returns (uint256) {
        PatientRecord storage patient = patientRecords[patientId];
        require(patient.exists, "Patient does not exist");
        require(msg.sender == patient.patientAddress || msg.sender == controller, "Not authorized");
        require(access != AccessLevel.None, "Invalid access level");
        require(durationSeconds > 0, "Duration required");
        uint256 oldPolicyId = patient.activePolicyId;
        if (oldPolicyId != 0) {
            policies[oldPolicyId].revoked = true;
            policies[oldPolicyId].isActive = false;
            policies[oldPolicyId].consent = false;
            emit PolicyRevoked(oldPolicyId);
        }
        patient.sensitivity = sensitivity;
        uint256 policyId = ++lastPolicyId;
        uint256 endTime = block.timestamp + durationSeconds;
        policies[policyId] = ConsentPolicy(policyId, patientId, purpose, access,
            true, consent, false, msg.sender, block.timestamp, endTime);
        patient.activePolicyId = policyId;
        emit PolicyCreated(policyId, patientId, purpose, access, consent, block.timestamp, endTime);
        emit ConsentParametersUpdated(patientId, oldPolicyId, policyId);
        return policyId;
    }
}
