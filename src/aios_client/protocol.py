"""Capability negotiation shared by setup and the outbound worker."""

V3_CAPABILITIES = {
    "environment-links-v3",
    "delegated-runs-v3",
    "character-recall-v3",
    "task-events-v3",
}


def negotiate(protocol, requested="auto"):
    versions = protocol.get("supported_versions", [protocol.get("version")])
    capabilities = set(protocol.get("capabilities", []))
    v3 = 3 in versions and V3_CAPABILITIES <= capabilities
    if requested == 3 and not v3:
        raise ValueError("AIOS must advertise protocol 3 and all v3 capabilities")
    if requested != 2 and v3:
        return 3
    if requested != 3 and 2 in versions:
        return 2
    raise ValueError("AIOS workspace protocol v2 or v3 is required")
