package com.blockpedia.exporter;

final class ExporterConstants {
    static final String MOD_ID = "blockpedia-exporter";
    static final String MINECRAFT_VERSION = "26.2";
    static final String EXPORT_CONTRACT_VERSION = "export-contract.v1";
    static final String EXPORTER_VERSION = "0.1.3";
    static final String STATE_POLICY_VERSION = "state-policy.v1";
    static final String RENDER_POLICY_VERSION = "render.v2";
    static final String FIXTURE_POLICY_VERSION = "fixture.v1";
    static final String DEDUPE_POLICY_VERSION = "dedupe.v1";
    static final String PRE_RENDER_SKIP_POLICY_TOKEN = "pre-render-skip.v1;reason=BLOCK_ENTITY_FIXTURE_UNSUPPORTED;ids=minecraft:end_gateway,minecraft:end_portal";
    static final String FIXTURE_ID = "isolated_default";
    static final int IMAGE_SIZE = 512;
    static final int QUADRANT_SIZE = 256;
    static final int FULL_BRIGHT = 0x00F000F0;
    static final int FULL_WHITE = 0xFFFFFFFF;

    private ExporterConstants() {
    }
}
