package com.blockpedia.exporter;

import net.minecraft.client.Minecraft;
import net.minecraft.resources.Identifier;
import net.minecraft.server.packs.PackType;
import net.minecraft.server.packs.PackResources;
import net.minecraft.server.packs.resources.Resource;
import net.minecraft.server.packs.resources.ResourceFilterSection;
import net.minecraft.server.packs.resources.ResourceManager;

import java.io.IOException;
import java.io.InputStream;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Predicate;

final class ResourceSnapshot {
    private final Map<String, byte[]> resources;
    private final String hash;

    private ResourceSnapshot(Map<String, byte[]> resources) {
        this.resources = Map.copyOf(resources);
        this.hash = JsonCanonical.sha256ResourceSnapshot(resources);
    }

    static ResourceSnapshot verify(Minecraft minecraft) throws IOException {
        ResourceManager resourceManager = minecraft.getResourceManager();
        verifyActivePackScope(resourceManager);
        Map<String, byte[]> contents = new LinkedHashMap<>();
        for (String prefix : List.of("blockstates", "models", "textures")) {
            for (Map.Entry<Identifier, List<Resource>> entry : resourceManager.listResourceStacks(
                prefix,
                scopedPredicate(prefix)
            ).entrySet()) {
                contents.put(entry.getKey().toString(), readVanillaWinner(entry.getKey(), entry.getValue()));
            }
        }

        for (String required : List.of(
            "minecraft:lang/en_us.json",
            "minecraft:lang/zh_cn.json"
        )) {
            Identifier identifier = Identifier.parse(required);
            List<Resource> stack = resourceManager.getResourceStack(identifier);
            if (stack.isEmpty()) {
                throw new IOException("missing required resource: " + required);
            }
            contents.put(required, readVanillaWinner(identifier, stack));
        }

        if (contents.isEmpty()) {
            throw new IOException("no gated vanilla resources were found");
        }
        return new ResourceSnapshot(contents);
    }

    private static void verifyActivePackScope(ResourceManager resourceManager) throws IOException {
        List<PackResources> packs;
        try {
            // PackResources are owned by ResourceManager; this stream is only enumerated, never closed.
            packs = resourceManager.listPacks().toList();
        } catch (RuntimeException exception) {
            throw new IOException("cannot enumerate active resource packs", exception);
        }

        PackResources vanilla = null;
        int vanillaCount = 0;
        try {
            for (PackResources pack : packs) {
                if ("vanilla".equals(pack.packId())) {
                    vanilla = pack;
                    vanillaCount++;
                }
            }
            if (vanillaCount != 1 || vanilla == null) {
                throw new IOException("active vanilla resource pack count is " + vanillaCount);
            }

            Set<Identifier> vanillaIdentifiers = vanillaScopeIdentifiers(vanilla);
            for (PackResources pack : packs) {
                if (!"vanilla".equals(pack.packId())) {
                    verifyNonVanillaPack(pack, vanillaIdentifiers);
                }
            }
        } catch (IOException exception) {
            throw exception;
        } catch (RuntimeException exception) {
            throw new IOException("active resource pack verification failed", exception);
        }
    }

    private static Set<Identifier> vanillaScopeIdentifiers(PackResources vanilla) throws IOException {
        Set<Identifier> identifiers = new LinkedHashSet<>();
        for (String prefix : List.of("blockstates", "models", "textures")) {
            try {
                vanilla.listResources(
                    PackType.CLIENT_RESOURCES,
                    "minecraft",
                    prefix,
                    (identifier, ignored) -> {
                        if (scopedPredicate(prefix).test(identifier)) {
                            identifiers.add(identifier);
                        }
                    }
                );
            } catch (RuntimeException exception) {
                throw new IOException("cannot enumerate vanilla resources for " + prefix, exception);
            }
        }
        identifiers.add(Identifier.parse("minecraft:lang/en_us.json"));
        identifiers.add(Identifier.parse("minecraft:lang/zh_cn.json"));
        return identifiers;
    }

    private static void verifyNonVanillaPack(PackResources pack, Set<Identifier> vanillaIdentifiers) throws IOException {
        String packId = pack.packId();
        ResourceFilterSection filter;
        try {
            filter = pack.getMetadataSection(ResourceFilterSection.TYPE);
        } catch (IOException | RuntimeException exception) {
            throw new IOException("cannot inspect active resource pack " + packId, exception);
        }
        if (filter != null) {
            if (filter.isNamespaceFiltered("minecraft")) {
                throw new NonVanillaResourceException(
                    "active resource pack filters namespace minecraft: " + packId
                );
            }
            for (Identifier identifier : vanillaIdentifiers) {
                if (filter.isPathFiltered(identifier.getPath())) {
                    throw new NonVanillaResourceException(
                        "active resource pack filters " + identifier + ": " + packId
                    );
                }
            }
        }

        for (String prefix : List.of("blockstates", "models", "textures")) {
            Set<Identifier> rawIdentifiers = new LinkedHashSet<>();
            try {
                pack.listResources(
                    PackType.CLIENT_RESOURCES,
                    "minecraft",
                    prefix,
                    (identifier, ignored) -> {
                        if (scopedPredicate(prefix).test(identifier)) {
                            rawIdentifiers.add(identifier);
                        }
                    }
                );
            } catch (RuntimeException exception) {
                throw new IOException("cannot enumerate active resource pack " + packId, exception);
            }
            if (!rawIdentifiers.isEmpty()) {
                throw new NonVanillaResourceException(
                    "active resource pack contributes " + rawIdentifiers.iterator().next() + ": " + packId
                );
            }
        }

        for (String path : List.of(
            "lang/en_us.json",
            "lang/zh_cn.json",
            "lang/en_us.json.mcmeta",
            "lang/zh_cn.json.mcmeta"
        )) {
            Identifier identifier = Identifier.fromNamespaceAndPath("minecraft", path);
            try {
                if (pack.getResource(PackType.CLIENT_RESOURCES, identifier) != null) {
                    throw new NonVanillaResourceException(
                        "active resource pack contributes " + identifier + ": " + packId
                    );
                }
            } catch (RuntimeException exception) {
                throw new IOException("cannot inspect active resource pack " + packId, exception);
            }
        }
    }

    static Predicate<Identifier> scopedPredicate(String pathPrefix) {
        return identifier -> "minecraft".equals(identifier.getNamespace())
            && (identifier.getPath().equals(pathPrefix)
                || identifier.getPath().startsWith(pathPrefix + "/"));
    }

    private static byte[] readVanillaWinner(Identifier identifier, List<Resource> stack) throws IOException {
        if (stack.isEmpty()) {
            throw new IOException("empty resource stack: " + identifier);
        }
        byte[] winner = null;
        for (Resource resource : stack) {
            if (!"vanilla".equals(resource.sourcePackId())) {
                throw new NonVanillaResourceException(
                    "resource contribution is not the builtin vanilla pack: "
                        + identifier + " from " + resource.sourcePackId()
                );
            }
            try (InputStream input = resource.open()) {
                winner = input.readAllBytes();
            }
        }
        if (winner == null) {
            throw new IOException("resource stack content could not be read: " + identifier);
        }
        return winner;
    }

    String hash() {
        return hash;
    }

    Map<String, byte[]> resources() {
        return resources;
    }

    static final class NonVanillaResourceException extends IOException {
        NonVanillaResourceException(String message) {
            super(message);
        }
    }
}
