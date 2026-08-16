package com.blockpedia.exporter;

import com.google.gson.JsonObject;
import com.mojang.blaze3d.GpuFormat;
import com.mojang.blaze3d.buffers.GpuBuffer;
import com.mojang.blaze3d.buffers.GpuBufferSlice;
import com.mojang.blaze3d.buffers.GpuFence;
import com.mojang.blaze3d.pipeline.TextureTarget;
import com.mojang.blaze3d.platform.NativeImage;
import com.mojang.blaze3d.systems.RenderSystem;
import com.mojang.blaze3d.vertex.PoseStack;
import net.minecraft.client.Minecraft;
import net.minecraft.client.color.block.BlockTintSource;
import net.minecraft.client.renderer.block.BlockAndTintGetter;
import net.minecraft.client.renderer.GameRenderer;
import net.minecraft.client.renderer.Projection;
import net.minecraft.client.renderer.ProjectionMatrixBuffer;
import net.minecraft.client.renderer.SubmitNodeStorage;
import net.minecraft.client.renderer.block.BlockModelRenderState;
import net.minecraft.client.renderer.block.BlockModelResolver;
import net.minecraft.client.renderer.block.model.BlockDisplayContext;
import net.minecraft.client.renderer.feature.FeatureRenderDispatcher;
import net.minecraft.client.renderer.texture.OverlayTexture;
import net.minecraft.client.renderer.texture.TextureAtlas;
import net.minecraft.data.AtlasIds;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.Registries;
import net.minecraft.world.level.CardinalLighting;
import net.minecraft.world.level.biome.Biome;
import net.minecraft.world.level.biome.Biomes;
import net.minecraft.world.level.lighting.LevelLightEngine;
import net.minecraft.world.level.block.entity.BlockEntity;
import net.minecraft.world.level.material.Fluids;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import org.joml.Matrix4fStack;
import org.joml.Vector4f;

final class RenderExporter {
    private final Minecraft minecraft;

    RenderExporter(Minecraft minecraft) {
        this.minecraft = minecraft;
    }

    RenderResult render(
        String variantId,
        BlockState state,
        RenderPaths.Location renderPath,
        Path renderDirectory
    ) throws IOException {
        RenderSystem.assertOnRenderThread();
        Files.createDirectories(renderDirectory);
        var previousColor = RenderSystem.outputColorTextureOverride;
        var previousDepth = RenderSystem.outputDepthTextureOverride;
        var previousFog = RenderSystem.getShaderFog();
        var previousScissor = new com.mojang.blaze3d.systems.ScissorState(RenderSystem.getScissorStateForRenderTypeDraws());
        NativeImage preview = new NativeImage(
            NativeImage.Format.RGBA,
            ExporterConstants.IMAGE_SIZE,
            ExporterConstants.IMAGE_SIZE,
            false
        );
        ProjectionMatrixBuffer projectionBuffer = new ProjectionMatrixBuffer("blockpedia-export-projection");
        Projection projection = new Projection();
        RenderSystem.backupProjectionMatrix();
        Matrix4fStack modelView = RenderSystem.getModelViewStack();
        modelView.pushMatrix();
        try {
            for (View view : View.values()) {
                renderView(state, variantId, view, projectionBuffer, projection, modelView, preview);
            }
            validatePreview(preview);
            Path previewPath = renderDirectory.resolve("preview.png");
            preview.writeToFile(previewPath);
            NativeImage mask = createMask(preview);
            Path maskPath = renderDirectory.resolve("mask.png");
            mask.writeToFile(maskPath);
            mask.close();

            String imageHash = JsonCanonical.sha256Bytes(Files.readAllBytes(previewPath));
            String maskHash = JsonCanonical.sha256Bytes(Files.readAllBytes(maskPath));
            JsonObject metadata = renderMetadata(
                variantId,
                tintSensitive(state)
            );
            Path metadataPath = renderDirectory.resolve("render.json");
            JsonCanonical.writeJson(metadataPath, metadata);
            String metadataHash = JsonCanonical.sha256(metadata);

            JsonObject renderReference = new JsonObject();
            renderReference.addProperty("render_policy_version", ExporterConstants.RENDER_POLICY_VERSION);
            renderReference.addProperty("preview_path", renderPath.artifact("preview.png"));
            renderReference.addProperty("mask_path", renderPath.artifact("mask.png"));
            renderReference.addProperty("render_metadata_path", renderPath.artifact("render.json"));
            renderReference.addProperty("image_sha256", imageHash);
            renderReference.addProperty("mask_sha256", maskHash);
            renderReference.addProperty("render_metadata_sha256", metadataHash);
            return new RenderResult(renderReference, metadata);
        } finally {
            try {
                RenderSystem.outputColorTextureOverride = previousColor;
                RenderSystem.outputDepthTextureOverride = previousDepth;
                RenderSystem.setShaderFog(previousFog);
                RenderSystem.getScissorStateForRenderTypeDraws().setFrom(previousScissor);
                modelView.popMatrix();
                RenderSystem.restoreProjectionMatrix();
            } finally {
                try {
                    projectionBuffer.close();
                } finally {
                    preview.close();
                }
            }
        }
    }

    private void renderView(
        BlockState state,
        String variantId,
        View view,
        ProjectionMatrixBuffer projectionBuffer,
        Projection projection,
        Matrix4fStack modelView,
        NativeImage preview
    ) throws IOException {
        TextureTarget target = new TextureTarget(
            "blockpedia-export-" + variantId + "-" + view.id,
            ExporterConstants.QUADRANT_SIZE,
            ExporterConstants.QUADRANT_SIZE,
            true,
            GpuFormat.RGBA8_UNORM
        );
        var previousViewColor = RenderSystem.outputColorTextureOverride;
        var previousViewDepth = RenderSystem.outputDepthTextureOverride;
        NativeImage image = null;
        try {
            RenderSystem.outputColorTextureOverride = target.getColorTextureView();
            RenderSystem.outputDepthTextureOverride = target.getDepthTextureView();
            clearTarget(target);
            renderState(state, projectionBuffer, projection, modelView, view);
            image = capture(target);
            validateView(image, view.id);
            copyInto(preview, image, view.originX, view.originY);
        } finally {
            if (image != null) {
                image.close();
            }
            RenderSystem.outputColorTextureOverride = previousViewColor;
            RenderSystem.outputDepthTextureOverride = previousViewDepth;
            target.destroyBuffers();
        }
    }

    private void clearTarget(TextureTarget target) {
        var encoder = RenderSystem.getDevice().createCommandEncoder();
        encoder.clearColorAndDepthTextures(
            target.getColorTexture(),
            new Vector4f(0.0f, 0.0f, 0.0f, 0.0f),
            target.getDepthTexture(),
            0.0
        );
        encoder.submit();
    }

    private void renderState(
        BlockState state,
        ProjectionMatrixBuffer projectionBuffer,
        Projection projection,
        Matrix4fStack modelView,
        View view
    ) throws IOException {
        projection.setupOrtho(-10.0f, 10.0f, 2.0f, 2.0f, false);
        GpuBufferSlice projectionSlice = projectionBuffer.getBuffer(projection);
        RenderSystem.setProjectionMatrix(projectionSlice, com.mojang.blaze3d.ProjectionType.ORTHOGRAPHIC);

        modelView.identity();
        modelView.translate(0.5f, 0.5f, 1.0f);
        modelView.translate(0.5f, 0.5f, 0.5f);
        modelView.rotateY((float) Math.toRadians(-view.yaw));
        modelView.rotateX((float) Math.toRadians(-view.pitch));
        modelView.translate(-0.5f, -0.5f, -0.5f);

        BlockModelResolver resolver = new BlockModelResolver(minecraft.getModelManager());
        BlockModelRenderState modelState = new BlockModelRenderState();
        resolver.update(modelState, state, BlockDisplayContext.create());
        List<BlockTintSource> tintSources = minecraft.getBlockColors().getTintSources(state);
        if (!tintSources.isEmpty()) {
            BlockAndTintGetter plains = FixedPlainsContext.create(minecraft);
            var tintLayers = modelState.tintLayers();
            tintLayers.clear();
            for (BlockTintSource source : tintSources) {
                tintLayers.add(source.colorInWorld(state, plains, BlockPos.ZERO));
            }
        }
        SubmitNodeStorage storage = new SubmitNodeStorage();
        PoseStack poses = new PoseStack();
        TextureAtlas blockAtlas = minecraft.getAtlasManager().getAtlasOrThrow(AtlasIds.BLOCKS);
        MissingMaterialTracker.begin(blockAtlas);
        try {
            modelState.submit(poses, storage, ExporterConstants.FULL_BRIGHT, OverlayTexture.NO_OVERLAY, 0);
        } finally {
            MissingMaterialTracker.end();
        }
        GameRenderer gameRenderer = minecraft.gameRenderer;
        FeatureRenderDispatcher dispatcher = gameRenderer.featureRenderDispatcher();
        FeatureRenderDispatcher.PreparedFrame prepared = dispatcher.prepareFrame(storage);
        try {
            prepared.executeSolid();
            prepared.executeTranslucent();
            prepared.executeTranslucentAfterTerrain();
            prepared.executeAlwaysOnTop();
        } finally {
            prepared.close();
        }
    }

    private NativeImage capture(TextureTarget target) throws IOException {
        int width = target.width;
        int height = target.height;
        int blockSize = target.getColorTexture().getFormat().blockSize();
        if (blockSize != 4) {
            throw new IOException("RGBA8 readback expected 4-byte pixels, got " + blockSize);
        }
        long bufferSize = (long) width * height * blockSize;
        GpuBuffer buffer = RenderSystem.getDevice().createBuffer(
            () -> "Blockpedia screenshot buffer",
            GpuBuffer.USAGE_COPY_DST | GpuBuffer.USAGE_MAP_READ,
            bufferSize
        );
        GpuFence fence = null;
        try {
            var encoder = RenderSystem.getDevice().createCommandEncoder();
            encoder.copyTextureToBuffer(target.getColorTexture(), buffer, 0L, () -> { }, 0);
            fence = encoder.createFence();
            encoder.submit();
            if (!fence.awaitCompletion(1_000_000_000L)) {
                throw new IOException("GPU readback did not complete");
            }
            NativeImage image = new NativeImage(NativeImage.Format.RGBA, width, height, false);
            try (var mapped = buffer.map(true, false)) {
                ByteBuffer data = mapped.data();
                for (int y = 0; y < height; y++) {
                    for (int x = 0; x < width; x++) {
                        int pixel = data.getInt((y * width + x) * blockSize);
                        image.setPixelABGR(x, height - y - 1, pixel);
                    }
                }
            } catch (Throwable throwable) {
                image.close();
                throw throwable;
            }
            return image;
        } finally {
            if (fence != null) {
                fence.close();
            }
            buffer.close();
        }
    }

    private void validateView(NativeImage image, String viewId) throws IOException {
        if (image.getWidth() != ExporterConstants.QUADRANT_SIZE
            || image.getHeight() != ExporterConstants.QUADRANT_SIZE) {
            throw new IOException("render view is not 256x256: " + viewId);
        }
        checkPixels(image, viewId);
    }

    private void copyInto(NativeImage destination, NativeImage source, int originX, int originY) {
        for (int y = 0; y < source.getHeight(); y++) {
            for (int x = 0; x < source.getWidth(); x++) {
                destination.setPixel(originX + x, originY + y, source.getPixel(x, y));
            }
        }
    }

    private void validatePreview(NativeImage image) throws IOException {
        if (image.getWidth() != ExporterConstants.IMAGE_SIZE || image.getHeight() != ExporterConstants.IMAGE_SIZE) {
            throw new IOException("render dimensions are not 512x512");
        }
        int nonTransparent = 0;
        boolean[] quadrantHasObject = new boolean[4];
        for (int y = 0; y < image.getHeight(); y++) {
            for (int x = 0; x < image.getWidth(); x++) {
                if ((image.getPixel(x, y) >>> 24) != 0) {
                    nonTransparent++;
                    int quadrant = (y < ExporterConstants.QUADRANT_SIZE ? 0 : 2)
                        + (x < ExporterConstants.QUADRANT_SIZE ? 0 : 1);
                    quadrantHasObject[quadrant] = true;
                }
            }
        }
        if (nonTransparent == 0) {
            throw new RenderValidationException("EMPTY_RENDER", "render is fully transparent");
        }
        int minimumObjectPixels = ExporterConstants.QUADRANT_SIZE * ExporterConstants.QUADRANT_SIZE / 256;
        if (nonTransparent < minimumObjectPixels * quadrantHasObject.length) {
            throw new RenderValidationException("OBJECT_TOO_SMALL", "render object is too small");
        }
        checkPixels(image, "preview");
        for (View view : View.values()) {
            NativeImage quadrant = new NativeImage(
                NativeImage.Format.RGBA,
                ExporterConstants.QUADRANT_SIZE,
                ExporterConstants.QUADRANT_SIZE,
                false
            );
            try {
                for (int y = 0; y < ExporterConstants.QUADRANT_SIZE; y++) {
                    for (int x = 0; x < ExporterConstants.QUADRANT_SIZE; x++) {
                        quadrant.setPixel(x, y, image.getPixel(view.originX + x, view.originY + y));
                    }
                }
                checkPixels(quadrant, view.id);
            } finally {
                quadrant.close();
            }
        }
    }

    private PixelCheck checkPixels(NativeImage image, String label) throws IOException {
        int nonTransparent = 0;
        int minX = image.getWidth();
        int minY = image.getHeight();
        int maxX = -1;
        int maxY = -1;
        for (int y = 0; y < image.getHeight(); y++) {
            for (int x = 0; x < image.getWidth(); x++) {
                int pixel = image.getPixel(x, y);
                int alpha = (pixel >>> 24) & 0xff;
                if (alpha == 0) {
                    continue;
                }
                nonTransparent++;
                minX = Math.min(minX, x);
                minY = Math.min(minY, y);
                maxX = Math.max(maxX, x);
                maxY = Math.max(maxY, y);
            }
        }
        if (nonTransparent > 0 && (minX == 0 || minY == 0 || maxX == image.getWidth() - 1 || maxY == image.getHeight() - 1)) {
            throw new RenderValidationException("OBJECT_OFF_CANVAS", "render object touches canvas boundary: " + label);
        }
        return new PixelCheck(nonTransparent);
    }

    private NativeImage createMask(NativeImage preview) {
        NativeImage mask = new NativeImage(NativeImage.Format.RGBA, ExporterConstants.IMAGE_SIZE, ExporterConstants.IMAGE_SIZE, false);
        for (int y = 0; y < ExporterConstants.IMAGE_SIZE; y++) {
            for (int x = 0; x < ExporterConstants.IMAGE_SIZE; x++) {
                int alpha = (preview.getPixel(x, y) >>> 24) & 0xff;
                mask.setPixel(x, y, (alpha << 24) | (alpha << 16) | (alpha << 8) | alpha);
            }
        }
        return mask;
    }

    private JsonObject renderMetadata(
        String variantId,
        boolean tintSensitive
    ) {
        JsonObject result = new JsonObject();
        result.addProperty("schema_version", "render-metadata.v1");
        result.addProperty("variant_id", variantId);
        result.addProperty("width", ExporterConstants.IMAGE_SIZE);
        result.addProperty("height", ExporterConstants.IMAGE_SIZE);
        result.addProperty("format", "PNG-RGBA");
        result.add("views", JsonCanonical.GSON.toJsonTree(List.of("isometric", "front", "side", "top")));
        result.addProperty("fixture_id", ExporterConstants.FIXTURE_ID);
        result.addProperty("fixture_version", ExporterConstants.FIXTURE_POLICY_VERSION);
        result.addProperty("tint_sensitive", tintSensitive);
        if (tintSensitive) {
            result.addProperty("baseline_biome", "minecraft:plains");
        } else {
            result.add("baseline_biome", com.google.gson.JsonNull.INSTANCE);
        }
        JsonObject mask = new JsonObject();
        mask.addProperty("present", true);
        mask.addProperty("format", "PNG-RGBA");
        mask.addProperty("channel", "alpha");
        mask.addProperty("threshold", 1);
        result.add("mask", mask);
        return result;
    }

    private boolean tintSensitive(BlockState state) {
        return !minecraft.getBlockColors().getTintSources(state).isEmpty();
    }

    static final class RenderResult {
        final JsonObject renderReference;
        final JsonObject metadata;

        RenderResult(JsonObject renderReference, JsonObject metadata) {
            this.renderReference = renderReference;
            this.metadata = metadata;
        }
    }

    private record PixelCheck(int nonTransparent) {
    }

    static final class RenderValidationException extends IOException {
        private final String reasonCode;

        RenderValidationException(String reasonCode, String message) {
            super(message);
            this.reasonCode = reasonCode;
        }

        String reasonCode() {
            return reasonCode;
        }
    }

    private static final class FixedPlainsContext implements BlockAndTintGetter {
        private final Biome plains;

        private FixedPlainsContext(Biome plains) {
            this.plains = plains;
        }

        static FixedPlainsContext create(Minecraft minecraft) throws IOException {
            if (minecraft.level == null) {
                throw new RenderValidationException("EXPORTER_EXCEPTION", "plains tint context requires a loaded client registry");
            }
            try {
                Biome plains = minecraft.level.registryAccess()
                    .lookupOrThrow(Registries.BIOME)
                    .getOrThrow(Biomes.PLAINS)
                    .value();
                return new FixedPlainsContext(plains);
            } catch (RuntimeException exception) {
                throw new RenderValidationException("EXPORTER_EXCEPTION", "minecraft:plains tint context is unavailable");
            }
        }

        @Override
        public CardinalLighting cardinalLighting() {
            return CardinalLighting.DEFAULT;
        }

        @Override
        public LevelLightEngine getLightEngine() {
            return LevelLightEngine.EMPTY;
        }

        @Override
        public int getBlockTint(BlockPos position, net.minecraft.world.level.ColorResolver resolver) {
            return resolver.getColor(plains, position.getX(), position.getZ());
        }

        @Override
        public BlockEntity getBlockEntity(BlockPos position) {
            return null;
        }

        @Override
        public BlockState getBlockState(BlockPos position) {
            return net.minecraft.world.level.block.Blocks.AIR.defaultBlockState();
        }

        @Override
        public net.minecraft.world.level.material.FluidState getFluidState(BlockPos position) {
            return Fluids.EMPTY.defaultFluidState();
        }

        @Override
        public int getHeight() {
            return 0;
        }

        @Override
        public int getMinY() {
            return 0;
        }
    }

    private enum View {
        ISOMETRIC("isometric", 45.0f, 30.0f, 0, 0),
        FRONT("front", 0.0f, 0.0f, ExporterConstants.QUADRANT_SIZE, 0),
        SIDE("side", 90.0f, 0.0f, 0, ExporterConstants.QUADRANT_SIZE),
        TOP("top", 0.0f, -90.0f, ExporterConstants.QUADRANT_SIZE, ExporterConstants.QUADRANT_SIZE);

        private final String id;
        private final float yaw;
        private final float pitch;
        private final int originX;
        private final int originY;

        View(String id, float yaw, float pitch, int originX, int originY) {
            this.id = id;
            this.yaw = yaw;
            this.pitch = pitch;
            this.originX = originX;
            this.originY = originY;
        }
    }
}
