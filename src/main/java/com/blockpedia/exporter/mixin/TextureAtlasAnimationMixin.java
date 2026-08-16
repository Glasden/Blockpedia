package com.blockpedia.exporter.mixin;

import com.blockpedia.exporter.AnimationFreezeGate;
import net.minecraft.client.renderer.texture.TextureAtlas;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(TextureAtlas.class)
public abstract class TextureAtlasAnimationMixin {
    @Inject(method = "cycleAnimationFrames", at = @At("HEAD"), cancellable = true)
    private void blockpedia$freezeBlockAtlasAnimation(CallbackInfo callbackInfo) {
        TextureAtlas atlas = (TextureAtlas) (Object) this;
        if (AnimationFreezeGate.isActive() && TextureAtlas.LOCATION_BLOCKS.equals(atlas.location())) {
            callbackInfo.cancel();
        }
    }
}
