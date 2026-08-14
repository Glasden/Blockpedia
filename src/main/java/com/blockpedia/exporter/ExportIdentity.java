package com.blockpedia.exporter;

import java.io.IOException;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DecimalStyle;
import java.util.Locale;

/** Allocates the public export directory name and its one staging directory. */
final class ExportIdentity {
    private static final DateTimeFormatter EXPORT_TIMESTAMP = DateTimeFormatter
        .ofPattern("yyyyMMdd'T'HHmmss'Z'", Locale.ROOT)
        .withDecimalStyle(DecimalStyle.STANDARD)
        .withZone(ZoneOffset.UTC);

    private ExportIdentity() {
    }

    static Allocation allocate(Path exportParent, Instant startedAt) throws IOException {
        Files.createDirectories(exportParent);
        String baseId = "export_" + EXPORT_TIMESTAMP.format(startedAt);
        for (int suffix = 0; suffix <= 99; suffix++) {
            String exportId = suffix == 0 ? baseId : baseId + "_" + twoDigits(suffix);
            Path finalDirectory = exportParent.resolve(exportId);
            Path stagingDirectory = exportParent.resolve("." + exportId + ".staging");
            if (occupied(finalDirectory) || occupied(stagingDirectory)) {
                continue;
            }
            try {
                Files.createDirectory(stagingDirectory);
            } catch (FileAlreadyExistsException collision) {
                continue;
            }
            if (occupied(finalDirectory)) {
                try {
                    Files.deleteIfExists(stagingDirectory);
                } catch (IOException cleanupFailure) {
                    throw new IOException(
                        "export ID reservation raced with final directory " + finalDirectory,
                        cleanupFailure
                    );
                }
                continue;
            }
            return new Allocation(exportId, stagingDirectory, finalDirectory);
        }
        throw new IOException("export ID conflict exhausted: " + baseId + " through " + baseId + "_99");
    }

    private static boolean occupied(Path path) {
        return Files.exists(path, LinkOption.NOFOLLOW_LINKS);
    }

    private static String twoDigits(int value) {
        return value < 10 ? "0" + value : Integer.toString(value);
    }

    static final class Allocation {
        final String exportId;
        final Path stagingDirectory;
        final Path finalDirectory;

        Allocation(String exportId, Path stagingDirectory, Path finalDirectory) {
            this.exportId = exportId;
            this.stagingDirectory = stagingDirectory;
            this.finalDirectory = finalDirectory;
        }
    }
}
