package com.blockpedia.exporter;

import com.google.gson.JsonObject;
import net.minecraft.client.Minecraft;

import java.util.Locale;

/** The one frozen render input snapshot used by manifest and render records. */
record RenderEnvironment(
    String osName,
    String osVersion,
    String architecture,
    String gpuVendor,
    String gpuModel,
    String driverVersion,
    String backend,
    String resolution,
    String cameraHash,
    String lightingHash,
    String backgroundHash,
    String backboardHash,
    String supportHash,
    String rendererOptions
) {
    static RenderEnvironment capture(Minecraft minecraft) throws java.io.IOException {
        String rawOs = System.getProperty("os.name", "");
        String osName = rawOs.toLowerCase(Locale.ROOT).contains("win") ? "Windows"
            : rawOs.toLowerCase(Locale.ROOT).contains("linux") ? "Linux" : rawOs;
        String architecture = System.getProperty("os.arch", "");
        if (!"Windows".equals(osName) && !"Linux".equals(osName)) {
            throw new java.io.IOException("unsupported exporter platform: " + rawOs);
        }
        if (!("amd64".equalsIgnoreCase(architecture)
            || "x86_64".equalsIgnoreCase(architecture)
            || "x86-64".equalsIgnoreCase(architecture))) {
            throw new java.io.IOException("unsupported exporter architecture: " + architecture);
        }
        RenderSystemInfo.Device device = RenderSystemInfo.device(minecraft);
        return new RenderEnvironment(
            osName,
            System.getProperty("os.version", "unknown"),
            "x86_64",
            device.vendor(),
            device.name(),
            device.driver(),
            device.backend(),
            "512x512",
            JsonCanonical.sha256String("camera.v1:projection=orthographic;extent=2.0x2.0;invertY=false;zNear=-10.0;zFar=10.0;modelView=identity,translate(0.5,0.5,1.0),translate(0.5,0.5,0.5),rotateY(-yaw),rotateX(-pitch),translate(-0.5,-0.5,-0.5);views=isometric(yaw=45.0,pitch=30.0),front(yaw=0.0,pitch=0.0),side(yaw=90.0,pitch=0.0),top(yaw=0.0,pitch=-90.0)"),
            JsonCanonical.sha256String("lighting.v1:full_bright:overlay=no_overlay:shader_disabled"),
            JsonCanonical.sha256String("background.v1:transparent"),
            JsonCanonical.sha256String("backboard.v1:none"),
            JsonCanonical.sha256String("fixture.v1:isolated_default:none"),
            "shader=disabled;post_processing=disabled;fov=70;camera=orthographic;"
                + "block_model_resolver_seed=42L;"
                + "block_atlas_reload=awaited_before_prepare;"
                + "block_atlas_animation=cycleAnimationFrames_cancelled_while_exporting"
        );
    }

    String hash() {
        return JsonCanonical.sha256(snapshot());
    }

    String renderInputSignature(String logicalInputSignature) {
        return JsonCanonical.sha256Framed(logicalInputSignature, hash());
    }

    JsonObject platformJson() {
        JsonObject result = new JsonObject();
        result.addProperty("os_name", osName);
        result.addProperty("os_version", osVersion);
        result.addProperty("architecture", architecture);
        result.addProperty("gpu_vendor", gpuVendor);
        result.addProperty("gpu_model", gpuModel);
        result.addProperty("driver_version", driverVersion);
        result.addProperty("render_backend", backend);
        result.addProperty("framebuffer_resolution", resolution);
        result.addProperty("render_environment_sha256", hash());
        return result;
    }

    JsonObject policyJson() {
        JsonObject result = new JsonObject();
        result.addProperty("camera_policy_version", "camera.v1");
        result.addProperty("camera_sha256", cameraHash);
        result.addProperty("lighting_policy_version", "lighting.v1");
        result.addProperty("lighting_sha256", lightingHash);
        result.addProperty("background_sha256", backgroundHash);
        result.addProperty("backboard_sha256", backboardHash);
        result.addProperty("support_fixture_sha256", supportHash);
        return result;
    }

    private JsonObject snapshot() {
        JsonObject result = new JsonObject();
        result.addProperty("os_name", osName);
        result.addProperty("os_version", osVersion);
        result.addProperty("architecture", architecture);
        result.addProperty("gpu_vendor", gpuVendor);
        result.addProperty("gpu_model", gpuModel);
        result.addProperty("driver_version", driverVersion);
        result.addProperty("render_backend", backend);
        result.addProperty("framebuffer_resolution", resolution);
        result.addProperty("camera_sha256", cameraHash);
        result.addProperty("lighting_sha256", lightingHash);
        result.addProperty("background_sha256", backgroundHash);
        result.addProperty("backboard_sha256", backboardHash);
        result.addProperty("support_fixture_sha256", supportHash);
        result.addProperty("renderer_options", rendererOptions);
        return result;
    }
}
