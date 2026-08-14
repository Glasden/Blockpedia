package com.blockpedia.exporter;

import com.mojang.brigadier.CommandDispatcher;
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
                .then(ClientCommands.literal("export").executes(context -> queueExport(context.getSource())))
        );
    }

    private static int queueExport(FabricClientCommandSource source) {
        if (activeJob != null) {
            source.sendError(Component.literal("Blockpedia export is already running."));
            return 0;
        }
        activeJob = new ExportJob(source.getClient());
        source.sendFeedback(Component.literal("Blockpedia export queued."));
        return 1;
    }

    private static void advanceOneStep(Minecraft minecraft) {
        ExportJob job = activeJob;
        if (job == null) {
            return;
        }
        try {
            if (job.advance()) {
                activeJob = null;
            }
        } catch (Throwable throwable) {
            job.fail(throwable);
            activeJob = null;
        }
    }

    private static final class ExportJob {
        private final Minecraft minecraft;
        private ExportPackage exportPackage;
        private Stage stage = Stage.QUEUED;

        private ExportJob(Minecraft minecraft) {
            this.minecraft = minecraft;
        }

        private boolean advance() throws IOException {
            switch (stage) {
                case QUEUED -> {
                    feedback("Blockpedia export running: EXPORT_REGISTRY");
                    exportPackage = ExportPackage.prepare(minecraft);
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
