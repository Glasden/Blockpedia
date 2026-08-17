package com.blockpedia.exporter;

import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.arguments.StringArgumentType;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.command.v2.ClientCommandRegistrationCallback;
import net.fabricmc.fabric.api.client.command.v2.ClientCommands;
import net.fabricmc.fabric.api.client.command.v2.FabricClientCommandSource;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.minecraft.client.Minecraft;
import net.minecraft.commands.CommandBuildContext;
import net.minecraft.network.chat.Component;

import java.io.IOException;
import java.nio.file.Path;
import java.util.concurrent.CompletableFuture;

public final class BlockpediaExporterClient implements ClientModInitializer {
    private static ExportJob activeJob;

    @Override
    public void onInitializeClient() {
        ClientCommandRegistrationCallback.EVENT.register(BlockpediaExporterClient::registerCommand);
        ClientTickEvents.END_CLIENT_TICK.register(BlockpediaExporterClient::advanceOneStep);
    }

    private static void registerCommand(
        CommandDispatcher<FabricClientCommandSource> dispatcher,
        CommandBuildContext ignored
    ) {
        dispatcher.register(
            ClientCommands.literal("blockindex")
                .then(ClientCommands.literal("export")
                    .executes(context -> queueExport(context.getSource()))
                    .then(ClientCommands.literal("banner-repair")
                        .then(ClientCommands.argument("base_export_id", StringArgumentType.word())
                            .executes(context -> queueBannerRepair(
                                context.getSource(),
                                StringArgumentType.getString(context, "base_export_id")
                            )))))
        );
    }

    private static int queueExport(FabricClientCommandSource source) {
        if (activeJob != null) {
            source.sendError(Component.literal("Blockpedia export is already running."));
            return 0;
        }
        try {
            activeJob = new ExportJob(source.getClient());
        } catch (Throwable throwable) {
            String message = throwable.getMessage() == null
                ? throwable.getClass().getSimpleName() : throwable.getMessage();
            source.sendError(Component.literal("Blockpedia export could not start: " + message));
            return 0;
        }
        source.sendFeedback(Component.literal("Blockpedia export queued."));
        return 1;
    }

    private static int queueBannerRepair(FabricClientCommandSource source, String baseExportId) {
        if (!ExportIdentity.isValidExportId(baseExportId)) {
            source.sendError(Component.literal("Invalid base export ID."));
            return 0;
        }
        if (activeJob != null) {
            source.sendError(Component.literal("Blockpedia export is already running."));
            return 0;
        }
        try {
            activeJob = new ExportJob(source.getClient(), baseExportId);
        } catch (Throwable throwable) {
            String message = throwable.getMessage() == null
                ? throwable.getClass().getSimpleName() : throwable.getMessage();
            source.sendError(Component.literal("Blockpedia banner repair could not start: " + message));
            return 0;
        }
        source.sendFeedback(Component.literal("Blockpedia banner repair queued for " + baseExportId + "."));
        return 1;
    }

    private static void advanceOneStep(Minecraft minecraft) {
        ExportJob job = activeJob;
        if (job == null) {
            return;
        }
        try {
            if (job.advance()) {
                job.clearAnimationFreezeGate();
                activeJob = null;
            }
        } catch (Throwable throwable) {
            try {
                job.fail(throwable);
            } finally {
                job.clearAnimationFreezeGate();
                activeJob = null;
            }
        }
    }

    private static final class ExportJob {
        private final Minecraft minecraft;
        private final String baseExportId;
        private final CompletableFuture<Void> resourceReload;
        private ExportPackage exportPackage;
        private Stage stage = Stage.QUEUED;
        private volatile boolean reloadComplete;
        private volatile Throwable reloadFailure;
        private boolean animationFreezeGateCleared;

        private ExportJob(Minecraft minecraft) {
            this(minecraft, null);
        }

        private ExportJob(Minecraft minecraft, String baseExportId) {
            this.minecraft = minecraft;
            this.baseExportId = baseExportId;
            boolean gateEnabled = false;
            try {
                AnimationFreezeGate.enable();
                gateEnabled = true;
                resourceReload = minecraft.reloadResourcePacks();
                resourceReload.whenComplete((ignored, failure) -> {
                    reloadFailure = failure;
                    reloadComplete = true;
                });
            } catch (Throwable throwable) {
                if (gateEnabled) {
                    AnimationFreezeGate.clear();
                }
                throw throwable;
            }
        }

        private boolean advance() throws IOException {
            switch (stage) {
                case QUEUED -> {
                    if (!reloadComplete) {
                        return false;
                    }
                    if (reloadFailure != null) {
                        throw new IOException("resource reload failed", reloadFailure);
                    }
                    feedback(baseExportId == null
                        ? "Blockpedia export running: EXPORT_REGISTRY"
                        : "Blockpedia banner repair running: EXPORT_REGISTRY");
                    exportPackage = baseExportId == null
                        ? ExportPackage.prepare(minecraft)
                        : ExportPackage.prepareBannerRepair(minecraft, baseExportId);
                    stage = Stage.EXPORT_REGISTRY;
                }
                case EXPORT_REGISTRY -> {
                    if (exportPackage.exportRegistryStep()) {
                        stage = Stage.SELECT_VARIANTS;
                    }
                }
                case SELECT_VARIANTS -> {
                    if (exportPackage.selectVariantStep()) {
                        stage = Stage.RENDER_VARIANTS;
                    }
                }
                case RENDER_VARIANTS -> {
                    if (exportPackage.renderVariantStep()) {
                        stage = Stage.FINISH;
                    }
                }
                case FINISH -> {
                    Path result = exportPackage.finish();
                    feedback("Blockpedia export " + (result.getFileName().toString().startsWith(".") ? "failed" : "succeeded/needs_review") + ": " + result);
                    return true;
                }
            }
            return false;
        }

        private void fail(Throwable throwable) {
            String message = throwable.getMessage() == null ? throwable.getClass().getSimpleName() : throwable.getMessage();
            if (exportPackage != null) {
                try {
                    message += " (staging: " + exportPackage.fail(throwable) + ")";
                } catch (IOException diagnosticFailure) {
                    message += " (staging preservation failed: " + diagnosticFailure.getMessage() + ")";
                }
            }
            minecraft.gui.hud.getChat().addClientSystemMessage(Component.literal("Blockpedia export failed: " + message));
        }

        private void clearAnimationFreezeGate() {
            if (!animationFreezeGateCleared) {
                animationFreezeGateCleared = true;
                AnimationFreezeGate.clear();
            }
        }

        private void feedback(String message) {
            minecraft.gui.hud.getChat().addClientSystemMessage(Component.literal(message));
        }
    }

    private enum Stage {
        QUEUED,
        EXPORT_REGISTRY,
        SELECT_VARIANTS,
        RENDER_VARIANTS,
        FINISH
    }
}
