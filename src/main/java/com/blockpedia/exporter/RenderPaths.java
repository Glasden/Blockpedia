package com.blockpedia.exporter;

import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/** Direct, non-sanitizing block-id to renders path derivation. */
final class RenderPaths {
    private static final Set<String> WINDOWS_DEVICE_NAMES = Set.of(
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
    );

    private RenderPaths() {
    }

    static Location forBlockId(String blockId) throws IOException {
        if (blockId == null || blockId.isEmpty()) {
            throw new IOException("block_id is empty");
        }
        int separator = blockId.indexOf(':');
        if (separator <= 0 || separator != blockId.lastIndexOf(':') || separator == blockId.length() - 1) {
            throw new IOException("block_id has no safe namespace/path split: " + blockId);
        }
        String namespace = blockId.substring(0, separator);
        String path = blockId.substring(separator + 1);
        validateSegment(namespace, blockId);
        if (path.indexOf('\\') >= 0 || path.indexOf('\u0000') >= 0) {
            throw new IOException("block_id contains an unsafe path separator: " + blockId);
        }

        List<String> segments = new ArrayList<>();
        segments.add(namespace);
        for (String segment : path.split("/", -1)) {
            validateSegment(segment, blockId);
            segments.add(segment);
        }
        return new Location(blockId, List.copyOf(segments));
    }

    private static void validateSegment(String segment, String blockId) throws IOException {
        if (segment.isEmpty() || ".".equals(segment) || "..".equals(segment)
            || segment.endsWith(".") || segment.endsWith(" ")) {
            throw new IOException("block_id contains an unsafe render path segment: " + blockId);
        }
        for (int index = 0; index < segment.length(); index++) {
            char character = segment.charAt(index);
            if (!((character >= 'a' && character <= 'z')
                || (character >= '0' && character <= '9')
                || character == '_' || character == '-' || character == '.')) {
                throw new IOException("block_id contains an unsafe render path segment: " + blockId);
            }
        }
        String deviceName = segment;
        int extension = deviceName.indexOf('.');
        if (extension >= 0) {
            deviceName = deviceName.substring(0, extension);
        }
        if (WINDOWS_DEVICE_NAMES.contains(deviceName.toUpperCase(Locale.ROOT))) {
            throw new IOException("block_id contains a Windows device name: " + blockId);
        }
    }

    static final class Location {
        private final String blockId;
        private final List<String> segments;

        private Location(String blockId, List<String> segments) {
            this.blockId = blockId;
            this.segments = segments;
        }

        Path directory(Path exportDirectory) throws IOException {
            Path rendersRoot = exportDirectory.resolve("renders").toAbsolutePath().normalize();
            Path directory = rendersRoot;
            for (String segment : segments) {
                directory = directory.resolve(segment);
            }
            directory = directory.normalize();
            if (!directory.startsWith(rendersRoot) || directory.equals(rendersRoot)) {
                throw new IOException("render path escapes renders/: " + blockId);
            }
            return directory;
        }

        String directoryRelative() {
            return "renders/" + String.join("/", segments);
        }

        String artifact(String fileName) {
            if (!"preview.png".equals(fileName)
                && !"mask.png".equals(fileName)
                && !"render.json".equals(fileName)) {
                throw new IllegalArgumentException("unsupported render artifact: " + fileName);
            }
            return directoryRelative() + "/" + fileName;
        }

        List<String> directoryPrefixes() {
            List<String> prefixes = new ArrayList<>();
            for (int count = 1; count <= segments.size(); count++) {
                prefixes.add("renders/" + String.join("/", segments.subList(0, count)));
            }
            return prefixes;
        }
    }
}
