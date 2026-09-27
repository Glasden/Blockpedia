package com.blockpedia.exporter;

import com.mojang.blaze3d.platform.Lighting;
import com.mojang.blaze3d.systems.RenderSystem;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.Identifier;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.CompletableFuture;

/** Isolated GPU check: build renderLightingProbeJar and launch with blockpedia.probe.output. */
public final class RenderLightingProbe implements ClientModInitializer {
    private int ticks;
    private CompletableFuture<Void> reload;
    private boolean done;
    @Override
    public void onInitializeClient() {
        ClientTickEvents.END_CLIENT_TICK.register(mc -> {
            if (done || mc.level == null || ++ticks < 60) return;
            if (reload == null) {
                AnimationFreezeGate.enable();
                reload = mc.reloadResourcePacks();
                return;
            }
            if (!reload.isDone()) return;
            done = true;
            Path output = Path.of(System.getProperty("blockpedia.probe.output"));
            try {
                reload.join();
                Files.createDirectories(output);
                JsonCanonical.writeJson(output.resolve("environment.json"), RenderEnvironment.capture(mc).platformJson());
                JsonCanonical.writeJson(output.resolve("policy.json"), RenderEnvironment.capture(mc).policyJson());
                Files.createDirectories(output.resolve("textures"));
                for (String texture : new String[]{"snow", "white_wool", "white_concrete", "quartz_block_side", "quartz_block_top", "oak_planks", "stripped_spruce_log", "stripped_spruce_log_top", "stone", "glass", "glowstone"}) {
                    try (var input = mc.getResourceManager().open(Identifier.parse("minecraft:textures/block/" + texture + ".png"))) {
                        Files.copy(input, output.resolve("textures").resolve(texture + ".png"));
                    }
                }
                RenderExporter exporter = new RenderExporter(mc);
                for (String name : new String[]{"snow_block", "white_wool", "white_concrete", "quartz_block", "oak_planks", "stripped_spruce_log", "stone", "glass", "oak_stairs", "brown_banner", "chest", "glowstone", "oak_leaves"}) {
                    String id = "minecraft:" + name;
                    var location = RenderPaths.forBlockId(id);
                    mc.gameRenderer.lighting().setupFor(Lighting.Entry.ITEMS_FLAT);
                    var previousLights = RenderSystem.getShaderLights();
                    exporter.render(id, BuiltInRegistries.BLOCK.getValue(Identifier.parse(id)).defaultBlockState(), location, location.directory(output));
                    if (RenderSystem.getShaderLights() != previousLights) throw new AssertionError("lighting was not restored: " + name);
                    mc.gameRenderer.lighting().setupFor(Lighting.Entry.ENTITY_IN_UI);
                    previousLights = RenderSystem.getShaderLights();
                    exporter.render(id, BuiltInRegistries.BLOCK.getValue(Identifier.parse(id)).defaultBlockState(), location, location.directory(output.resolve("alternate")));
                    if (RenderSystem.getShaderLights() != previousLights) throw new AssertionError("alternate lighting was not restored: " + name);
                    if (Files.mismatch(location.directory(output).resolve("preview.png"), location.directory(output.resolve("alternate")).resolve("preview.png")) != -1) throw new AssertionError("inherited lighting changes preview: " + name);
                    System.out.println("COLOR_PROBE_RENDERED " + name);
                }
                // Exercise cleanup after rendering succeeds but PNG persistence fails.
                String id = "minecraft:snow_block";
                var location = RenderPaths.forBlockId(id);
                var state = BuiltInRegistries.BLOCK.getValue(Identifier.parse(id)).defaultBlockState();
                var blocked = location.directory(output.resolve("write-failure"));
                Files.createDirectories(blocked.resolve("preview.png"));
                var previousLights = RenderSystem.getShaderLights();
                try {
                    exporter.render(id, state, location, blocked);
                    throw new AssertionError("expected PNG write failure");
                } catch (IOException expected) {
                    if (RenderSystem.getShaderLights() != previousLights) {
                        throw new AssertionError("lighting was not restored after failure");
                    }
                }
                exporter.render(id, state, location, location.directory(output.resolve("recovered")));
                if (Files.mismatch(location.directory(output).resolve("preview.png"),
                        location.directory(output.resolve("recovered")).resolve("preview.png")) != -1) {
                    throw new AssertionError("export changed after failure recovery");
                }
                Files.writeString(output.resolve("PASS.txt"), "13 samples; inherited lighting independence, restoration, write failure and recovery passed\n");
                System.out.println("COLOR_PROBE_COMPLETE " + output);
            } catch (Throwable e) {
                e.printStackTrace();
                try { Files.writeString(output.resolve("FAIL.txt"), e.toString()); } catch (Exception ignored) {}
            } finally {
                AnimationFreezeGate.clear();
                mc.stop();
            }
        });
    }
}
