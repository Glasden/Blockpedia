package com.blockpedia.exporter;

import net.fabricmc.fabric.api.client.renderer.v1.mesh.Mesh;
import net.fabricmc.fabric.api.client.renderer.v1.mesh.QuadAtlas;
import net.fabricmc.fabric.api.client.renderer.v1.mesh.QuadView;
import net.fabricmc.fabric.api.client.renderer.v1.sprite.FabricTextureAtlas;
import net.fabricmc.fabric.api.client.renderer.v1.sprite.SpriteFinder;
import net.minecraft.client.renderer.block.dispatch.BlockStateModelPart;
import net.minecraft.client.renderer.texture.TextureAtlas;
import net.minecraft.client.renderer.texture.TextureAtlasSprite;
import net.minecraft.client.resources.model.geometry.BakedQuad;
import net.minecraft.core.Direction;

import java.util.List;

/**
 * Per-submit material authority for the exporter render scope.
 *
 * <p>The tracker deliberately observes the resolved submission rather than
 * rendered colors. It is installed only around one block-model submission and
 * is never retained between views or exports.</p>
 */
public final class MissingMaterialTracker {
    private static final ThreadLocal<Scope> ACTIVE = new ThreadLocal<>();

    private MissingMaterialTracker() {
    }

    public static void begin(TextureAtlas blockAtlas) {
        if (ACTIVE.get() != null) {
            throw new IllegalStateException("nested missing-material render scope");
        }
        ACTIVE.set(new Scope(blockAtlas));
    }

    public static void end() {
        ACTIVE.remove();
    }

    public static void inspectVanillaParts(List<BlockStateModelPart> parts)
        throws RenderExporter.RenderValidationException {
        Scope scope = ACTIVE.get();
        if (scope != null) {
            scope.inspectParts(parts);
        }
    }

    public static void inspectFabricSubmission(List<BlockStateModelPart> parts, Mesh mesh)
        throws RenderExporter.RenderValidationException {
        Scope scope = ACTIVE.get();
        if (scope != null) {
            scope.inspectParts(parts);
            scope.inspectMesh(mesh);
        }
    }

    private static final class Scope {
        private final TextureAtlasSprite missingSprite;
        private final SpriteFinder blockSpriteFinder;

        private Scope(TextureAtlas blockAtlas) {
            this.missingSprite = blockAtlas.missingSprite();
            this.blockSpriteFinder = ((FabricTextureAtlas) (Object) blockAtlas).spriteFinder();
        }

        private void inspectParts(List<BlockStateModelPart> parts)
            throws RenderExporter.RenderValidationException {
            if (parts == null) {
                return;
            }
            for (BlockStateModelPart part : parts) {
                if (part == null) {
                    continue;
                }
                var particle = part.particleMaterial();
                if (particle != null && particle.sprite() == missingSprite) {
                    throw missingMaterial();
                }
                for (Direction direction : Direction.values()) {
                    inspectQuads(part.getQuads(direction));
                }
                inspectQuads(part.getQuads(null));
            }
        }

        private void inspectQuads(List<BakedQuad> quads)
            throws RenderExporter.RenderValidationException {
            if (quads == null) {
                return;
            }
            for (BakedQuad quad : quads) {
                if (quad != null && quad.materialInfo() != null
                    && quad.materialInfo().sprite() == missingSprite) {
                    throw missingMaterial();
                }
            }
        }

        private void inspectMesh(Mesh mesh) throws RenderExporter.RenderValidationException {
            if (mesh == null) {
                return;
            }
            final RenderExporter.RenderValidationException[] failure = {null};
            mesh.forEach(quad -> {
                if (failure[0] != null || quad == null || quad.atlas() != QuadAtlas.BLOCK) {
                    return;
                }
                if (!uvsWithinMissingSprite(quad)) {
                    return;
                }
                TextureAtlasSprite resolved = blockSpriteFinder.find(quad);
                if (resolved == missingSprite) {
                    failure[0] = missingMaterial();
                }
            });
            if (failure[0] != null) {
                throw failure[0];
            }
        }

        private boolean uvsWithinMissingSprite(QuadView quad) {
            for (int index = 0; index < 4; index++) {
                float u = quad.u(index);
                float v = quad.v(index);
                if (u < missingSprite.getU0() || u > missingSprite.getU1()
                    || v < missingSprite.getV0() || v > missingSprite.getV1()) {
                    return false;
                }
            }
            return true;
        }

        private RenderExporter.RenderValidationException missingMaterial() {
            return new RenderExporter.RenderValidationException(
                "MISSING_TEXTURE",
                "resolved block material is the block-atlas missing sprite"
            );
        }
    }
}
