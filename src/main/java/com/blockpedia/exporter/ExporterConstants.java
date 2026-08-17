package com.blockpedia.exporter;

import java.util.List;

final class ExporterConstants {
    static final String MOD_ID = "blockpedia-exporter";
    static final String MINECRAFT_VERSION = "26.2";
    static final String EXPORT_CONTRACT_VERSION = "export-contract.v1";
    static final String EXPORTER_VERSION = "0.1.4";
    static final String STATE_POLICY_VERSION = "state-policy.v1";
    static final String RENDER_POLICY_VERSION = "render.v2";
    static final String FIXTURE_POLICY_VERSION = "fixture.v1";
    static final String DEDUPE_POLICY_VERSION = "dedupe.v1";
    static final String PRE_RENDER_SKIP_POLICY_TOKEN = "pre-render-skip.v1;reason=BLOCK_ENTITY_FIXTURE_UNSUPPORTED;ids=minecraft:end_gateway,minecraft:end_portal";
    static final String CAMERA_POLICY_VERSION = "camera.v2";
    static final String BANNER_CAMERA_POLICY_TOKEN = "banner-camera.v2;namespace=minecraft;types=BannerBlock,WallBannerBlock;colors=black,blue,brown,cyan,gray,green,light_blue,light_gray,lime,magenta,orange,pink,purple,red,white,yellow;forms=banner,wall_banner";
    static final String BANNER_PARENT_TRANSFORM = "translate(0.5,0.5,0.5);scale(0.72,0.72,0.72);translate(-0.5,-0.5,-0.5)";
    static final List<String> BANNER_REPAIR_TARGET_IDS = List.of(
        "minecraft:black_banner", "minecraft:black_wall_banner",
        "minecraft:blue_banner", "minecraft:blue_wall_banner",
        "minecraft:brown_banner", "minecraft:brown_wall_banner",
        "minecraft:cyan_banner", "minecraft:cyan_wall_banner",
        "minecraft:gray_banner", "minecraft:gray_wall_banner",
        "minecraft:green_banner", "minecraft:green_wall_banner",
        "minecraft:light_blue_banner", "minecraft:light_blue_wall_banner",
        "minecraft:light_gray_banner", "minecraft:light_gray_wall_banner",
        "minecraft:lime_banner", "minecraft:lime_wall_banner",
        "minecraft:magenta_banner", "minecraft:magenta_wall_banner",
        "minecraft:orange_banner", "minecraft:orange_wall_banner",
        "minecraft:pink_banner", "minecraft:pink_wall_banner",
        "minecraft:purple_banner", "minecraft:purple_wall_banner",
        "minecraft:red_banner", "minecraft:red_wall_banner",
        "minecraft:white_banner", "minecraft:white_wall_banner",
        "minecraft:yellow_banner", "minecraft:yellow_wall_banner"
    );
    static final float BANNER_PARENT_SCALE = 0.72f;
    static final String FIXTURE_ID = "isolated_default";
    static final int IMAGE_SIZE = 512;
    static final int QUADRANT_SIZE = 256;
    static final int FULL_BRIGHT = 0x00F000F0;
    static final int FULL_WHITE = 0xFFFFFFFF;

    private ExporterConstants() {
    }

    static boolean isBannerRepairTarget(String blockId) {
        return BANNER_REPAIR_TARGET_IDS.contains(blockId);
    }
}
