package com.blockpedia.exporter;

import java.util.concurrent.atomic.AtomicBoolean;

/** Client-only export scope used by the atlas animation mixin. */
public final class AnimationFreezeGate {
    private static final AtomicBoolean ACTIVE = new AtomicBoolean();

    private AnimationFreezeGate() {
    }

    public static void enable() {
        if (!ACTIVE.compareAndSet(false, true)) {
            throw new IllegalStateException("Blockpedia export animation gate is already active");
        }
    }

    public static void clear() {
        ACTIVE.set(false);
    }

    public static boolean isActive() {
        return ACTIVE.get();
    }
}
