package com.blockpedia.exporter.mixin;

import com.blockpedia.exporter.MissingMaterialTracker;
import com.mojang.blaze3d.vertex.PoseStack;
import net.fabricmc.fabric.api.client.renderer.v1.mesh.Mesh;
import net.minecraft.client.renderer.SubmitNodeStorage;
import net.minecraft.client.renderer.block.dispatch.BlockStateModelPart;
import net.minecraft.client.renderer.chunk.ChunkSectionLayer;
import net.minecraft.client.renderer.rendertype.RenderType;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

import java.util.List;
import java.util.function.Function;

@Mixin(SubmitNodeStorage.class)
public abstract class SubmitNodeStorageMaterialMixin {
    @Inject(
        method = "submitBlockModel(Lcom/mojang/blaze3d/vertex/PoseStack;Lnet/minecraft/client/renderer/rendertype/RenderType;Ljava/util/List;[IIII)V",
        at = @At("HEAD")
    )
    private void blockpedia$inspectVanillaSubmission(
        PoseStack poses,
        RenderType renderType,
        List<BlockStateModelPart> parts,
        int[] tintLayers,
        int lightCoords,
        int overlayCoords,
        int outlineColor,
        CallbackInfo callbackInfo
    ) throws Exception {
        MissingMaterialTracker.inspectVanillaParts(parts);
    }

    @Inject(
        method = "submitBlockModel(Lcom/mojang/blaze3d/vertex/PoseStack;Ljava/util/function/Function;ZLjava/util/List;Lnet/fabricmc/fabric/api/client/renderer/v1/mesh/Mesh;[IIII)V",
        at = @At("HEAD"),
        remap = false
    )
    private void blockpedia$inspectFabricSubmission(
        PoseStack poses,
        Function<ChunkSectionLayer, RenderType> renderTypeFunction,
        boolean translucent,
        List<BlockStateModelPart> parts,
        Mesh mesh,
        int[] tintLayers,
        int lightCoords,
        int overlayCoords,
        int outlineColor,
        CallbackInfo callbackInfo
    ) throws Exception {
        MissingMaterialTracker.inspectFabricSubmission(parts, mesh);
    }
}
