package com.blockpedia.exporter;

import org.joml.Matrix4f;
import org.joml.Vector3f;

/** Run with gradlew verifyRenderCamera; no GPU or test framework required. */
public final class RenderCameraCheck {
    public static void main(String[] args) {
        Matrix4f matrix = new Matrix4f();
        for (RenderExporter.View view : RenderExporter.View.values()) {
            view.setupModelView(matrix);
            assert point(matrix, .5f, .5f, .5f).distance(1, 1, 1.5f) < 1e-5f : view + " center moved";
            assert Math.abs(matrix.determinant() - 1) < 1e-5f : view + " mirrors face winding";
        }

        RenderExporter.View.FRONT.setupModelView(matrix);
        assert depth(matrix, .5f, .5f, 0) > depth(matrix, .5f, .5f, 1) : "front must see north (furnace opening)";
        assert point(matrix, .5f, 1, .5f).y > point(matrix, .5f, 0, .5f).y : "front must be upright";

        RenderExporter.View.SIDE.setupModelView(matrix);
        assert depth(matrix, 1, .5f, .5f) > depth(matrix, 0, .5f, .5f) : "side must see east";

        RenderExporter.View.TOP.setupModelView(matrix);
        assert depth(matrix, .5f, 1, .5f) > depth(matrix, .5f, 0, .5f) : "top must see roof, not bottom";

        RenderExporter.View.ISOMETRIC.setupModelView(matrix);
        assert depth(matrix, .5f, 1, .5f) > depth(matrix, .5f, 0, .5f) : "isometric must look down";
        assert depth(matrix, .5f, .5f, 0) > depth(matrix, .5f, .5f, 1) : "isometric must see north";
        assert depth(matrix, 1, .5f, .5f) > depth(matrix, 0, .5f, .5f) : "isometric must see east";
        assert Math.abs(point(matrix, .5f, 1, .5f).x - point(matrix, .5f, 0, .5f).x) < 1e-5f : "verticals must stay vertical";
        System.out.println("Render camera checks passed (four views, front/side/top occlusion and handedness).");
    }

    private static Vector3f point(Matrix4f matrix, float x, float y, float z) {
        return matrix.transformPosition(new Vector3f(x, y, z));
    }

    private static float depth(Matrix4f matrix, float x, float y, float z) {
        // Minecraft's reversed-Z projection and GREATER_OR_EQUAL test retain larger view-space Z.
        return point(matrix, x, y, z).z;
    }
}
