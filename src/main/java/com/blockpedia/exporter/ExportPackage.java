package com.blockpedia.exporter;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.mojang.blaze3d.platform.NativeImage;
import net.minecraft.client.Minecraft;
import net.minecraft.resources.Identifier;
import net.minecraft.world.level.block.state.BlockState;

import java.io.BufferedWriter;
import java.io.IOException;
import java.io.InputStream;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.FileSystemException;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Stream;

final class ExportPackage {
    private final Minecraft minecraft;
    private final String exportId;
    private final Path stagingDirectory;
    private final Path finalDirectory;
    private final String exporterVersion;
    private final Instant startedAt;
    private final ResourceSnapshot resourceSnapshot;
    private final RenderEnvironment renderEnvironment;
    private final String logicalInputSignature;
    private final String renderInputSignature;
    private final List<Identifier> blockIds;
    private final List<ExportRecords.BlockData> blocks;
    private final List<ExportFailure> failures;
    private final List<JsonObject> variants;
    private RenderExporter renderer;
    private int registryIndex;
    private int selectIndex;
    private int renderIndex;
    private boolean registryStarted;
    private boolean registryEnded;
    private boolean selectStarted;
    private boolean selectEnded;
    private boolean renderStarted;
    private boolean renderEnded;

    private ExportPackage(
        Minecraft minecraft,
        String exportId,
        Path stagingDirectory,
        Path finalDirectory,
        String exporterVersion,
        Instant startedAt,
        ResourceSnapshot resourceSnapshot,
        RenderEnvironment renderEnvironment,
        String logicalInputSignature,
        String renderInputSignature,
        List<Identifier> blockIds,
        List<ExportRecords.BlockData> blocks,
        List<ExportFailure> failures,
        List<JsonObject> variants
    ) {
        this.minecraft = minecraft;
        this.exportId = exportId;
        this.stagingDirectory = stagingDirectory;
        this.finalDirectory = finalDirectory;
        this.exporterVersion = exporterVersion;
        this.startedAt = startedAt;
        this.resourceSnapshot = resourceSnapshot;
        this.renderEnvironment = renderEnvironment;
        this.logicalInputSignature = logicalInputSignature;
        this.renderInputSignature = renderInputSignature;
        this.blockIds = List.copyOf(blockIds);
        this.blocks = blocks;
        this.failures = failures;
        this.variants = variants;
    }

    static ExportPackage prepare(Minecraft minecraft) throws IOException {
        JsonCanonical.selfCheck();
        Instant startedAt = Instant.now();
        Path dataRoot = minecraft.gameDirectory.toPath().resolve("blockpedia-data");
        Path exportParent = dataRoot.resolve("exports").resolve(ExporterConstants.MINECRAFT_VERSION);
        ExportIdentity.Allocation allocation = ExportIdentity.allocate(exportParent, startedAt);
        String exportId = allocation.exportId;
        Path finalDirectory = allocation.finalDirectory;
        Path stagingDirectory = allocation.stagingDirectory;
        Files.createDirectories(stagingDirectory.resolve("renders"));
        Files.writeString(
            stagingDirectory.resolve("exporter.log"),
            "PREPARE start\n",
            StandardCharsets.UTF_8,
            StandardOpenOption.CREATE,
            StandardOpenOption.TRUNCATE_EXISTING,
            StandardOpenOption.WRITE
        );
        ResourceSnapshot snapshot;
        try {
            snapshot = ResourceSnapshot.verify(minecraft);
        } catch (IOException exception) {
            Files.writeString(
                stagingDirectory.resolve("exporter.log"),
                "PREPARE failed: " + safeMessage(exception) + "\n",
                StandardCharsets.UTF_8,
                StandardOpenOption.APPEND,
                StandardOpenOption.WRITE
            );
            throw new IOException("resource gate failed; staging retained at " + stagingDirectory, exception);
        }
        RenderEnvironment renderEnvironment = RenderEnvironment.capture(minecraft);

        List<Identifier> ids = ExportRecords.minecraftBlockIds();
        String registrySignature = JsonCanonical.sha256String(
            String.join("\n", ids.stream().map(Identifier::toString).toList())
        );
        String logicalInputSignature = JsonCanonical.sha256Framed(
            ExporterConstants.MINECRAFT_VERSION,
            ExporterConstants.EXPORT_CONTRACT_VERSION,
            ExporterConstants.STATE_POLICY_VERSION,
            ExporterConstants.RENDER_POLICY_VERSION,
            ExporterConstants.FIXTURE_POLICY_VERSION,
            ExporterConstants.DEDUPE_POLICY_VERSION,
            snapshot.hash(),
            registrySignature
        );
        String renderInputSignature = renderEnvironment.renderInputSignature(logicalInputSignature);
        return new ExportPackage(
            minecraft,
            exportId,
            stagingDirectory,
            finalDirectory,
            ExporterConstants.EXPORTER_VERSION,
            startedAt,
            snapshot,
            renderEnvironment,
            logicalInputSignature,
            renderInputSignature,
            ids,
            new ArrayList<>(),
            new ArrayList<>(),
            new ArrayList<>()
        );
    }

    boolean exportRegistryStep() throws IOException {
        if (!registryStarted) {
            registryStarted = true;
            appendLog("EXPORT_REGISTRY start");
        }
        if (registryIndex >= blockIds.size()) {
            if (!registryEnded) {
                registryEnded = true;
                appendLog("EXPORT_REGISTRY end blocks=" + blocks.size());
            }
            return true;
        }

        Identifier id = blockIds.get(registryIndex++);
        try {
            blocks.add(ExportRecords.collectBlock(minecraft, exportId, exporterVersion, id));
        } catch (RuntimeException exception) {
            failures.add(ExportFailure.exportFailure(
                exportId,
                exporterVersion,
                "EXPORTER_EXCEPTION",
                "registry record failed for " + id + ": " + safeMessage(exception),
                logicalInputSignature,
                Instant.now()
            ));
        }
        if (registryIndex >= blockIds.size()) {
            registryEnded = true;
            appendLog("EXPORT_REGISTRY end blocks=" + blocks.size());
            return true;
        }
        return false;
    }

    boolean selectVariantStep() {
        if (!selectStarted) {
            selectStarted = true;
            appendLog("SELECT_VARIANTS start");
        }
        if (selectIndex >= blocks.size()) {
            if (!selectEnded) {
                selectEnded = true;
                appendLog("SELECT_VARIANTS end variants=" + blocks.size());
            }
            return true;
        }

        ExportRecords.BlockData block = blocks.get(selectIndex++);
        List<String> representedStateIds = block.states.stream()
            .map(state -> state.stateId)
            .sorted(JsonCanonical.utf8Comparator())
            .toList();
        String variantId = block.blockId.toString();
        ExportRecords.VariantData variant = new ExportRecords.VariantData(
            variantId,
            block.blockId.toString(),
            block.defaultStateId,
            representedStateIds
        );
        block.variant = variant;
        try {
            RenderPaths.forBlockId(block.blockId.toString());
            for (ExportRecords.StateData state : block.states) {
                state.variantId = variantId;
                state.mappingStatus = "skipped";
            }
        } catch (IOException unsafePath) {
            variant.status = "skipped";
            variant.skipReasonCode = "EXPORTER_EXCEPTION";
            variant.skipReason = "block_id cannot be used as a safe render path: " + safeMessage(unsafePath);
            for (ExportRecords.StateData state : block.states) {
                state.variantId = null;
                state.mappingStatus = "skipped";
            }
        }
        if (selectIndex >= blocks.size()) {
            selectEnded = true;
            appendLog("SELECT_VARIANTS end variants=" + blocks.size());
            return true;
        }
        return false;
    }

    boolean renderVariantStep() {
        if (!renderStarted) {
            renderStarted = true;
            renderer = new RenderExporter(minecraft);
            appendLog("RENDER_VARIANTS start");
        }
        if (renderIndex >= blocks.size()) {
            if (!renderEnded) {
                renderEnded = true;
                appendLog("RENDER_VARIANTS end selected=" + selectedVariantCount());
            }
            return true;
        }

        ExportRecords.BlockData block = blocks.get(renderIndex++);
        ExportRecords.VariantData variant = block.variant;
        if ("skipped".equals(variant.status)) {
            variants.add(variant.toJson(exportId, exporterVersion));
            failures.add(ExportFailure.skipVariant(
                exportId,
                exporterVersion,
                block.blockId.toString(),
                variant.variantId,
                variant.skipReasonCode,
                variant.skipReason,
                renderInputSignature,
                Instant.now()
            ));
            if (renderIndex >= blocks.size()) {
                renderEnded = true;
                appendLog("RENDER_VARIANTS end selected=" + selectedVariantCount());
                return true;
            }
            return false;
        }

        RenderPaths.Location renderPath = null;
        try {
            renderPath = RenderPaths.forBlockId(block.blockId.toString());
            Path renderDirectory = renderPath.directory(stagingDirectory);
            BlockState defaultState = block.block.defaultBlockState();
            RenderExporter.RenderResult render = renderer.render(
                variant.variantId,
                defaultState,
                renderPath,
                renderDirectory
            );
            variant.status = "selected";
            variant.render = render.renderReference;
            variant.machineFacts = machineFacts(block);
            for (ExportRecords.StateData state : block.states) {
                state.mappingStatus = "mapped";
            }
            variants.add(variant.toJson(exportId, exporterVersion));
        } catch (Exception exception) {
            try {
                if (renderPath != null) {
                    deletePartialRenderDirectory(renderPath.directory(stagingDirectory));
                }
            } catch (IOException cleanupFailure) {
                exception.addSuppressed(cleanupFailure);
            }
            variant.status = "skipped";
            variant.skipReasonCode = reasonCode(exception);
            variant.skipReason = "ordinary block model render failed: " + safeMessage(exception);
            for (ExportRecords.StateData state : block.states) {
                state.variantId = null;
                state.mappingStatus = "skipped";
            }
            variants.add(variant.toJson(exportId, exporterVersion));
            failures.add(ExportFailure.skipVariant(
                exportId,
                exporterVersion,
                block.blockId.toString(),
                variant.variantId,
                variant.skipReasonCode,
                variant.skipReason,
                renderInputSignature,
                Instant.now()
            ));
        }
        if (renderIndex >= blocks.size()) {
            renderEnded = true;
            appendLog("RENDER_VARIANTS end selected=" + selectedVariantCount());
            return true;
        }
        return false;
    }

    private long selectedVariantCount() {
        return variants.stream()
            .filter(item -> "selected".equals(item.get("status").getAsString()))
            .count();
    }

    Path finish() throws IOException {
        Instant completedAt = Instant.now();
        appendLog("export finished status=" + status());
        writeRecords();
        JsonObject finalManifest = manifest(completedAt);
        JsonCanonical.writeJson(stagingDirectory.resolve("manifest.json"), finalManifest);
        writeChecksums();
        forceStagingFiles();

        List<String> gateIssues = CommitValidator.validate(stagingDirectory, finalManifest, blockIds);
        if (!gateIssues.isEmpty()) {
            String reasonCode = gateIssues.stream().anyMatch(issue -> issue.startsWith("checksum:"))
                ? "CHECKSUM_MISMATCH" : "SCHEMA_INVALID";
            failures.add(ExportFailure.exportFailure(
                exportId,
                exporterVersion,
                reasonCode,
                truncateMessage("commit gate failed: " + summarizeIssues(gateIssues)),
                logicalInputSignature,
                completedAt
            ));
            appendLog("commit gate failed: " + summarizeIssues(gateIssues));
            writeRecords();
            JsonCanonical.writeJson(stagingDirectory.resolve("manifest.json"), manifest(completedAt));
            writeChecksums();
            forceStagingFiles();
            return stagingDirectory;
        }
        if ("failed".equals(finalManifest.get("status").getAsString())) {
            return stagingDirectory;
        }
        if (Files.exists(finalDirectory)) {
            throw new IOException("export final directory appeared before atomic commit: " + finalDirectory);
        }
        try {
            Files.move(stagingDirectory, finalDirectory, StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException exception) {
            String detail = atomicMoveFailureDetail(exception);
            appendLog("atomic export commit failed: " + detail);
            throw new IOException("atomic export commit failed: " + detail, exception);
        }
        return finalDirectory;
    }

    Path fail(Throwable throwable) throws IOException {
        Instant completedAt = Instant.now();
        failures.add(ExportFailure.exportFailure(
            exportId,
            exporterVersion,
            "EXPORTER_EXCEPTION",
            "export aborted: " + safeMessage(throwable),
            logicalInputSignature,
            completedAt
        ));
        appendLog("export aborted: " + safeMessage(throwable));
        writeRecords();
        JsonCanonical.writeJson(stagingDirectory.resolve("manifest.json"), manifest(completedAt));
        writeChecksums();
        forceStagingFiles();
        return stagingDirectory;
    }

    private void writeRecords() throws IOException {
        try (BufferedWriter writer = Files.newBufferedWriter(
            stagingDirectory.resolve("blocks.jsonl"), StandardCharsets.UTF_8,
            StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING, StandardOpenOption.WRITE
        )) {
            for (ExportRecords.BlockData block : blocks) {
                JsonCanonical.appendJsonLine(writer, block.blockJson);
            }
        }
        try (BufferedWriter writer = Files.newBufferedWriter(
            stagingDirectory.resolve("states.jsonl"), StandardCharsets.UTF_8,
            StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING, StandardOpenOption.WRITE
        )) {
            for (ExportRecords.BlockData block : blocks) {
                for (ExportRecords.StateData state : block.states) {
                    JsonCanonical.appendJsonLine(writer, state.toJson());
                }
            }
        }
        try (BufferedWriter writer = Files.newBufferedWriter(
            stagingDirectory.resolve("variants.jsonl"), StandardCharsets.UTF_8,
            StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING, StandardOpenOption.WRITE
        )) {
            for (JsonObject variant : variants) {
                JsonCanonical.appendJsonLine(writer, variant);
            }
        }
        try (BufferedWriter writer = Files.newBufferedWriter(
            stagingDirectory.resolve("failures.jsonl"), StandardCharsets.UTF_8,
            StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING, StandardOpenOption.WRITE
        )) {
            for (ExportFailure failure : failures) {
                JsonCanonical.appendJsonLine(writer, failure.toJson());
            }
        }
    }

    private JsonObject manifest(Instant completedAt) {
        JsonObject result = new JsonObject();
        result.addProperty("schema_version", "export-manifest.v1");
        result.addProperty("export_contract_version", ExporterConstants.EXPORT_CONTRACT_VERSION);
        result.addProperty("export_id", exportId);
        result.addProperty("logical_input_signature", logicalInputSignature);
        result.addProperty("render_input_signature", renderInputSignature);
        result.addProperty("status", status());
        result.addProperty("created_at", JsonCanonical.timestamp(startedAt));
        result.addProperty("completed_at", JsonCanonical.timestamp(completedAt));
        result.add("toolchain", toolchain());
        result.add("runtime", runtime());
        result.add("platform", platform());
        result.add("render_environment", renderEnvironment());
        result.add("policies", policies());
        result.add("schema_inventory", schemaInventory());
        JsonObject scope = new JsonObject();
        scope.addProperty("namespace", "minecraft");
        scope.addProperty("registry", "block");
        scope.addProperty("registry_snapshot_sha256", registrySnapshotHash());
        result.add("scope", scope);
        result.add("counts", counts());
        result.add("files", files());
        JsonObject integrity = new JsonObject();
        integrity.addProperty("algorithm", "SHA-256");
        integrity.addProperty("checksum_file", "checksums.sha256");
        integrity.addProperty("canonical_json", "JCS-RFC8785");
        integrity.addProperty("jsonl_record_terminator", "LF");
        result.add("integrity", integrity);
        return result;
    }

    private String status() {
        Set<String> expectedBlockIds = blockIds.stream().map(Identifier::toString).collect(java.util.stream.Collectors.toSet());
        Set<String> actualBlockIds = new HashSet<>();
        boolean duplicateBlock = false;
        for (ExportRecords.BlockData block : blocks) {
            duplicateBlock |= !actualBlockIds.add(block.blockId.toString());
        }
        if (duplicateBlock || !actualBlockIds.equals(expectedBlockIds) || variants.size() != blocks.size()) {
            return "failed";
        }
        if (!failures.isEmpty() && failures.stream().anyMatch(failure -> "fail_export".equals(failure.toJson().get("action").getAsString()))) {
            return "failed";
        }
        return failures.isEmpty() ? "succeeded" : "needs_review";
    }

    private JsonObject toolchain() {
        JsonObject result = new JsonObject();
        result.addProperty("minecraft_edition", "Java");
        result.addProperty("minecraft_version", ExporterConstants.MINECRAFT_VERSION);
        result.addProperty("java_version", "25");
        result.addProperty("fabric_loader_version", "0.19.3");
        result.addProperty("fabric_api_version", "0.157.0+26.2");
        result.addProperty("loom_version", "1.17.19");
        result.addProperty("gradle_version", "9.5.1");
        result.addProperty("mappings", "Minecraft 26.2 native Mojang names (unobfuscated); no external mappings artifact");
        result.addProperty("exporter_mod_id", ExporterConstants.MOD_ID);
        result.addProperty("exporter_version", exporterVersion);
        return result;
    }

    private JsonObject runtime() {
        JsonObject result = new JsonObject();
        result.addProperty("resource_pack_id", "vanilla");
        result.addProperty("resource_pack_sha256", resourceSnapshot.hash());
        result.addProperty("language_primary", "zh_cn");
        result.addProperty("language_secondary", "en_us");
        result.addProperty("shader", "disabled");
        result.addProperty("world_fixture_version", ExporterConstants.FIXTURE_POLICY_VERSION);
        result.addProperty("biome", "minecraft:plains");
        result.addProperty("weather", "clear");
        result.addProperty("world_time", 6000);
        result.addProperty("fov", 70.0);
        result.addProperty("gui_scale", minecraft.getWindow().getGuiScale());
        result.addProperty("render_distance", minecraft.options.renderDistance().get());
        return result;
    }

    private JsonObject platform() {
        return renderEnvironment.platformJson();
    }

    private JsonObject renderEnvironment() {
        return renderEnvironment.policyJson();
    }

    private JsonObject policies() {
        JsonObject result = new JsonObject();
        result.addProperty("state_policy_version", ExporterConstants.STATE_POLICY_VERSION);
        result.addProperty("render_policy_version", ExporterConstants.RENDER_POLICY_VERSION);
        result.addProperty("fixture_policy_version", ExporterConstants.FIXTURE_POLICY_VERSION);
        result.addProperty("dedupe_policy_version", ExporterConstants.DEDUPE_POLICY_VERSION);
        return result;
    }

    private JsonArray schemaInventory() {
        List<String> ids = List.of(
            "export-block.v1", "export-failure.v1", "export-manifest.v1",
            "export-state.v1", "export-variant.v1", "render-metadata.v1"
        );
        JsonArray result = new JsonArray();
        for (String id : ids) {
            String repositoryPath = "schemas/exporter/" + id + ".json";
            JsonObject item = new JsonObject();
            item.addProperty("schema_id", id);
            item.addProperty("schema_sha256", schemaHash(id));
            item.addProperty("repository_path", repositoryPath);
            result.add(item);
        }
        return result;
    }

    private String schemaHash(String schemaId) {
        String resourcePath = "/" + "schemas/exporter/" + schemaId + ".json";
        try (var stream = ExportPackage.class.getResourceAsStream(resourcePath)) {
            if (stream == null) {
                throw new IllegalStateException("schema is not packaged: " + resourcePath);
            }
            return JsonCanonical.sha256Bytes(stream.readAllBytes());
        } catch (IOException exception) {
            throw new IllegalStateException("cannot read packaged schema: " + resourcePath, exception);
        }
    }

    private String registrySnapshotHash() {
        return JsonCanonical.sha256String(
            String.join("\n", blockIds.stream().map(Identifier::toString).toList())
        );
    }

    private JsonObject counts() {
        JsonObject result = new JsonObject();
        result.addProperty("registry_blocks", blockIds.size());
        result.addProperty("block_records", blocks.size());
        result.addProperty("state_records", blocks.stream().mapToInt(block -> block.states.size()).sum());
        result.addProperty("selected_variant_records", variants.stream().filter(item -> "selected".equals(item.get("status").getAsString())).count());
        result.addProperty("skipped_variant_records", variants.stream().filter(item -> "skipped".equals(item.get("status").getAsString())).count());
        result.addProperty("failure_records", failures.size());
        result.addProperty("pending_review_records", failures.stream().filter(failure -> "pending".equals(failure.toJson().get("review_status").getAsString())).count());
        return result;
    }

    private JsonObject files() {
        JsonObject result = new JsonObject();
        JsonObject blocks = descriptor("jsonl", "export-block.v1");
        JsonObject states = descriptor("jsonl", "export-state.v1");
        JsonObject variants = descriptor("jsonl", "export-variant.v1");
        JsonObject failures = descriptor("jsonl", "export-failure.v1");
        JsonObject checksum = descriptor("checksum", null);
        JsonObject log = descriptor("log", null);
        JsonObject renders = descriptor("render_directory", null);
        result.add("blocks.jsonl", blocks);
        result.add("states.jsonl", states);
        result.add("variants.jsonl", variants);
        result.add("failures.jsonl", failures);
        result.add("checksums.sha256", checksum);
        result.add("exporter.log", log);
        result.add("renders/", renders);
        return result;
    }

    private JsonObject descriptor(String kind, String schema) {
        JsonObject result = new JsonObject();
        result.addProperty("required", true);
        result.addProperty("kind", kind);
        if (schema != null) {
            result.addProperty("record_schema", schema);
            result.addProperty("line_hash_format", "64-lowercase-hex-without-prefix");
        }
        return result;
    }

    private JsonObject machineFacts(ExportRecords.BlockData block) {
        ExportRecords.StateData defaultState = block.states.stream()
            .filter(state -> state.isDefault)
            .findFirst()
            .orElseThrow();
        JsonObject result = new JsonObject();
        result.addProperty("geometry_signature", defaultState.shape.get("signature").getAsString());
        result.addProperty("collision_signature", defaultState.collision.get("signature").getAsString());
        result.add("shape", defaultState.shape);
        result.add("collision", defaultState.collision);
        result.addProperty("behavior_fingerprint", JsonCanonical.sha256(defaultState.behavior));
        JsonObject behaviorByState = new JsonObject();
        for (ExportRecords.StateData state : block.states) {
            behaviorByState.add(state.stateId, state.behavior);
        }
        result.add("behavior_by_state", behaviorByState);
        return result;
    }

    private void writeChecksums() throws IOException {
        List<Path> files;
        try (Stream<Path> stream = Files.walk(stagingDirectory)) {
            files = stream
                .filter(Files::isRegularFile)
                .filter(path -> !path.equals(stagingDirectory.resolve("checksums.sha256")))
                .sorted(Comparator.comparing(path -> stagingDirectory.relativize(path).toString().replace('\\', '/'), JsonCanonical.utf8Comparator()))
                .toList();
        }
        StringBuilder checksum = new StringBuilder();
        for (Path file : files) {
            checksum.append(JsonCanonical.sha256Bytes(Files.readAllBytes(file)).substring("sha256:".length()))
                .append("  ")
                .append(stagingDirectory.relativize(file).toString().replace('\\', '/'))
                .append('\n');
        }
        Files.writeString(
            stagingDirectory.resolve("checksums.sha256"), checksum.toString(), StandardCharsets.UTF_8,
            StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING, StandardOpenOption.WRITE
        );
    }

    private void forceStagingFiles() throws IOException {
        try (Stream<Path> stream = Files.walk(stagingDirectory)) {
            for (Path file : stream.filter(Files::isRegularFile).toList()) {
                try (FileChannel channel = FileChannel.open(file, StandardOpenOption.READ)) {
                    channel.force(true);
                }
            }
        }
    }

    private void deletePartialRenderDirectory(Path variantDirectory) throws IOException {
        Path rendersRoot = stagingDirectory.resolve("renders").toAbsolutePath().normalize();
        variantDirectory = variantDirectory.toAbsolutePath().normalize();
        if (!variantDirectory.startsWith(rendersRoot) || variantDirectory.equals(rendersRoot)) {
            throw new IOException("unsafe partial render path: " + variantDirectory);
        }
        if (!Files.exists(variantDirectory)) {
            return;
        }
        try (Stream<Path> stream = Files.walk(variantDirectory)) {
            for (Path path : stream.sorted(Comparator.reverseOrder()).toList()) {
                Files.deleteIfExists(path);
            }
        }
        Path parent = variantDirectory.getParent();
        while (parent != null && !parent.equals(rendersRoot)) {
            try {
                Files.delete(parent);
            } catch (java.nio.file.DirectoryNotEmptyException ignored) {
                break;
            }
            parent = parent.getParent();
        }
    }

    /** Commit-only validator for the six exporter records and their final bytes. */
    private static final class CommitValidator {
        private static final int MAX_ISSUES = 64;

        private static List<String> validate(Path directory, JsonObject manifest, List<Identifier> registry) {
            List<String> issues = new ArrayList<>();
            try {
                JsonCanonical.selfCheck();
                JsonObject rereadManifest = com.google.gson.JsonParser.parseString(
                    Files.readString(directory.resolve("manifest.json"), StandardCharsets.UTF_8)
                ).getAsJsonObject();
                if (!JsonCanonical.canonical(rereadManifest).equals(JsonCanonical.canonical(manifest))) {
                    issues.add("manifest:writer bytes changed");
                }
                List<JsonObject> blocks = readJsonl(directory.resolve("blocks.jsonl"), issues);
                List<JsonObject> states = readJsonl(directory.resolve("states.jsonl"), issues);
                List<JsonObject> variants = readJsonl(directory.resolve("variants.jsonl"), issues);
                List<JsonObject> failures = readJsonl(directory.resolve("failures.jsonl"), issues);
                if (blocks.size() != registry.size()) {
                    issues.add("registry:block count");
                }
                Set<String> ids = new HashSet<>();
                for (Identifier identifier : registry) {
                    ids.add(identifier.toString());
                }
                Set<String> actual = new HashSet<>();
                for (JsonObject block : blocks) {
                    String id = property(block, "block_id");
                    if (!actual.add(id) || !ids.contains(id)) {
                        issues.add("registry:block id " + id);
                    }
                }
                if (!actual.equals(ids)) {
                    issues.add("registry:coverage");
                }
                Set<String> selected = new HashSet<>();
                Set<String> skipped = new HashSet<>();
                Set<String> failureRefs = new HashSet<>();
                Set<String> stateIds = new HashSet<>();
                Set<String> variantIds = new HashSet<>();
                Map<String, JsonObject> blockById = new java.util.HashMap<>();
                Map<String, List<JsonObject>> statesByBlock = new java.util.HashMap<>();
                Map<String, JsonObject> stateById = new java.util.HashMap<>();
                Map<String, List<JsonObject>> variantsByBlock = new java.util.HashMap<>();
                Map<String, JsonObject> variantById = new java.util.HashMap<>();
                for (JsonObject block : blocks) {
                    blockById.put(property(block, "block_id"), block);
                }
                for (JsonObject state : states) {
                    String blockId = property(state, "block_id");
                    String stateId = property(state, "state_id");
                    if (!stateIds.add(stateId)) {
                        issues.add("state:duplicate " + stateId);
                    }
                    statesByBlock.computeIfAbsent(blockId, ignored -> new ArrayList<>()).add(state);
                    stateById.put(stateId, state);
                }
                for (JsonObject variant : variants) {
                    String variantId = property(variant, "variant_id");
                    String blockId = property(variant, "block_id");
                    if (!variantIds.add(variantId)) {
                        issues.add("variant:duplicate " + variantId);
                    }
                    if (!variantId.equals(blockId)) {
                        issues.add("variant:id differs from block " + variantId);
                    }
                    variantsByBlock.computeIfAbsent(blockId, ignored -> new ArrayList<>()).add(variant);
                    variantById.put(variantId, variant);
                    if (!blockById.containsKey(blockId)) {
                        issues.add("variant:block reference " + variantId);
                    }
                    String status = property(variant, "status");
                    if ("selected".equals(status)) {
                        selected.add(variantId);
                        validateSelected(directory, variant, issues);
                        if (variant.has("skip_reason_code") || variant.has("skip_reason")) {
                            issues.add("variant:selected skip fields " + variantId);
                        }
                    } else if ("skipped".equals(status)) {
                        skipped.add(variantId);
                        if (variant.has("canonical_state_id") || variant.has("represented_state_ids")
                            || variant.has("context") || variant.has("selection")
                            || variant.has("machine_facts") || variant.has("render")) {
                            issues.add("variant:skipped selected fields " + variantId);
                        }
                    } else {
                        issues.add("variant:status " + variantId);
                    }
                }
                for (JsonObject failure : failures) {
                    String variantId = failure.has("variant_id") ? property(failure, "variant_id") : "";
                    if (!variantId.isEmpty()) {
                        failureRefs.add(variantId);
                    }
                    if (!"pending".equals(property(failure, "review_status"))
                        && !"not_required".equals(property(failure, "review_status"))) {
                        issues.add("failure:review status");
                    }
                    if ("pending".equals(property(failure, "review_status")) && variantId.isEmpty()) {
                        issues.add("failure:pending reference");
                    }
                    if ("pending".equals(property(failure, "review_status"))
                        && (!"skip".equals(property(failure, "kind"))
                            || !"needs_review".equals(property(failure, "action")))) {
                        issues.add("failure:pending semantics");
                    }
                    checkFailureReference(failure, blockById, stateById, variantById, issues);
                }
                for (String variantId : skipped) {
                    if (!failureRefs.contains(variantId)) {
                        issues.add("variant:skipped failure " + variantId);
                    }
                }
                for (JsonObject block : blocks) {
                    String blockId = property(block, "block_id");
                    List<JsonObject> blockStates = statesByBlock.getOrDefault(blockId, List.of());
                    List<JsonObject> blockVariants = variantsByBlock.getOrDefault(blockId, List.of());
                    String defaultStateId = property(block, "default_state_id");
                    long defaults = blockStates.stream().filter(state -> state.get("is_default").getAsBoolean()).count();
                    if (blockStates.isEmpty() || !stateById.containsKey(defaultStateId) || defaults != 1) {
                        issues.add("state:default " + blockId);
                    }
                    if (blockVariants.size() != 1) {
                        issues.add("variant:count " + blockId);
                        continue;
                    }
                    JsonObject variant = blockVariants.get(0);
                    String variantId = property(variant, "variant_id");
                    for (JsonObject state : blockStates) {
                        String mapping = property(state, "mapping_status");
                        JsonArray references = state.getAsJsonArray("variant_ids");
                        if ("selected".equals(property(variant, "status"))) {
                            if (!"mapped".equals(mapping) || references.size() != 1 || !variantId.equals(references.get(0).getAsString())) {
                                issues.add("state:mapping " + property(state, "state_id"));
                            }
                        } else if (!"skipped".equals(mapping) || references.size() != 0) {
                            issues.add("state:skipped mapping " + property(state, "state_id"));
                        }
                        if (!property(state, "state_id").equals(defaultStateId) && state.get("is_default").getAsBoolean()) {
                            issues.add("state:duplicate default " + blockId);
                        }
                    }
                    if ("selected".equals(property(variant, "status"))) {
                        if (!defaultStateId.equals(property(variant, "canonical_state_id"))) {
                            issues.add("variant:canonical default " + blockId);
                        }
                        Set<String> represented = new HashSet<>();
                        variant.getAsJsonArray("represented_state_ids").forEach(item -> represented.add(item.getAsString()));
                        Set<String> legal = new HashSet<>(blockStates.stream().map(state -> property(state, "state_id")).toList());
                        if (!represented.equals(legal)) {
                            issues.add("variant:represented states " + blockId);
                        }
                    }
                }
                Path rendersRoot = directory.resolve("renders");
                Set<String> expectedRenderDirectories = new HashSet<>();
                Set<String> expectedRenderFiles = new HashSet<>();
                for (String selectedId : selected) {
                    try {
                        RenderPaths.Location location = RenderPaths.forBlockId(selectedId);
                        expectedRenderDirectories.addAll(location.directoryPrefixes());
                        expectedRenderFiles.add(location.artifact("preview.png"));
                        expectedRenderFiles.add(location.artifact("mask.png"));
                        expectedRenderFiles.add(location.artifact("render.json"));
                    } catch (IOException exception) {
                        issues.add("render:unsafe selected path " + selectedId);
                    }
                }
                Set<String> actualRenderDirectories = new HashSet<>();
                Set<String> actualRenderFiles = new HashSet<>();
                try (Stream<Path> stream = Files.walk(rendersRoot)) {
                    stream.filter(path -> !path.equals(rendersRoot)).forEach(path -> {
                        String relative = directory.relativize(path).toString().replace('\\', '/');
                        if (Files.isDirectory(path)) {
                            actualRenderDirectories.add(relative);
                        } else if (Files.isRegularFile(path)) {
                            actualRenderFiles.add(relative);
                        }
                    });
                }
                if (!actualRenderDirectories.equals(expectedRenderDirectories)) {
                    issues.add("render:directory set");
                }
                if (!actualRenderFiles.equals(expectedRenderFiles)) {
                    issues.add("render:file set");
                }
                for (String skippedId : skipped) {
                    try {
                        Path skippedDirectory = RenderPaths.forBlockId(skippedId).directory(directory);
                        if (Files.exists(skippedDirectory)) {
                            issues.add("render:skipped directory " + skippedId);
                        }
                    } catch (IOException ignored) {
                        // Unsafe skipped IDs have no derived path to inspect.
                    }
                }
                JsonObject manifestCounts = manifest.getAsJsonObject("counts");
                checkCount(manifestCounts, "registry_blocks", registry.size(), issues);
                checkCount(manifestCounts, "block_records", blocks.size(), issues);
                checkCount(manifestCounts, "state_records", states.size(), issues);
                checkCount(manifestCounts, "selected_variant_records", selected.size(), issues);
                checkCount(manifestCounts, "skipped_variant_records", skipped.size(), issues);
                checkCount(manifestCounts, "failure_records", failures.size(), issues);
                long pendingFailures = failures.stream()
                    .filter(failure -> "pending".equals(property(failure, "review_status"))).count();
                checkCount(manifestCounts, "pending_review_records", pendingFailures, issues);
                String expectedStatus = failures.stream().anyMatch(failure -> "fail_export".equals(property(failure, "action")))
                    || blocks.size() != registry.size() || states.isEmpty() ? "failed"
                    : pendingFailures > 0 ? "needs_review" : "succeeded";
                if (!expectedStatus.equals(property(rereadManifest, "status"))) {
                    issues.add("manifest:status");
                }
                if (manifest.getAsJsonObject("counts").get("registry_blocks").getAsInt() != registry.size()) {
                    issues.add("manifest:registry count");
                }
            } catch (Exception exception) {
                issues.add("schema:commit validator " + safeMessage(exception));
            }
            return issues.size() > MAX_ISSUES ? issues.subList(0, MAX_ISSUES) : issues;
        }

        private static List<JsonObject> readJsonl(Path path, List<String> issues) throws IOException {
            List<JsonObject> records = new ArrayList<>();
            byte[] raw = Files.readAllBytes(path);
            if (raw.length > 0 && raw[raw.length - 1] != '\n') {
                issues.add("schema:JSONL final LF " + path.getFileName());
            }
            if (new String(raw, StandardCharsets.UTF_8).contains("\r")) {
                issues.add("schema:JSONL CR " + path.getFileName());
            }
            for (String line : new String(raw, StandardCharsets.UTF_8).split("\\n", -1)) {
                if (line.isBlank()) {
                    if (!line.isEmpty()) {
                        issues.add("schema:blank JSONL line " + path.getFileName());
                    }
                    continue;
                }
                if (line.isBlank()) {
                    issues.add("schema:blank JSONL line " + path.getFileName());
                    continue;
                }
                try {
                    records.add(com.google.gson.JsonParser.parseString(line).getAsJsonObject());
                } catch (RuntimeException exception) {
                    issues.add("schema:invalid JSONL " + path.getFileName());
                }
            }
            return records;
        }

        private static void validateSelected(Path directory, JsonObject variant, List<String> issues) throws IOException {
            JsonObject render = variant.getAsJsonObject("render");
            String variantId = property(variant, "variant_id");
            RenderPaths.Location location;
            try {
                location = RenderPaths.forBlockId(variantId);
            } catch (IOException exception) {
                issues.add("render:unsafe path " + variantId);
                return;
            }
            String[] names = {"preview_path", "mask_path", "render_metadata_path"};
            for (String name : names) {
                String relative = property(render, name);
                Path path = directory.toAbsolutePath().normalize().resolve(relative).normalize();
                String expected = location.artifact(switch (name) {
                    case "preview_path" -> "preview.png";
                    case "mask_path" -> "mask.png";
                    default -> "render.json";
                });
                if (!expected.equals(relative)
                    || !path.startsWith(directory.toAbsolutePath().normalize())
                    || !Files.isRegularFile(path)) {
                    issues.add("render:file " + relative);
                }
            }
            Path variantDirectory = location.directory(directory);
            if (!Files.isDirectory(variantDirectory)) {
                issues.add("render:directory " + variantId);
                return;
            }
            try (Stream<Path> stream = Files.list(variantDirectory)) {
                List<String> namesInDirectory = stream.map(path -> path.getFileName().toString()).sorted().toList();
                if (!namesInDirectory.equals(List.of("mask.png", "preview.png", "render.json"))) {
                    issues.add("render:file set " + variantId);
                }
            }
            Path metadataPath = location.directory(directory).resolve("render.json");
            JsonObject metadata = com.google.gson.JsonParser.parseString(
                Files.readString(metadataPath, StandardCharsets.UTF_8)
            ).getAsJsonObject();
            if (metadata.get("width").getAsInt() != 512 || metadata.get("height").getAsInt() != 512) {
                issues.add("render:dimensions " + variantId);
            }
            if (!variantId.equals(property(metadata, "variant_id"))) {
                issues.add("render:metadata variant " + variantId);
            }
            if (!metadata.getAsJsonArray("views").toString().equals("[\"isometric\",\"front\",\"side\",\"top\"]")) {
                issues.add("render:views " + variantId);
            }
            boolean tintSensitive = metadata.has("tint_sensitive") && metadata.get("tint_sensitive").getAsBoolean();
            boolean baselineIsNull = metadata.has("baseline_biome") && metadata.get("baseline_biome").isJsonNull();
            if ((tintSensitive && !"minecraft:plains".equals(property(metadata, "baseline_biome")))
                || (!tintSensitive && !baselineIsNull)) {
                issues.add("render:tint baseline " + variantId);
            }
            Path previewPath = location.directory(directory).resolve("preview.png");
            Path maskPath = location.directory(directory).resolve("mask.png");
            validatePng(previewPath, issues);
            validatePng(maskPath, issues);
            if (!property(render, "image_sha256").equals(JsonCanonical.sha256Bytes(Files.readAllBytes(previewPath)))) {
                issues.add("render:preview hash " + variantId);
            }
            if (!property(render, "mask_sha256").equals(JsonCanonical.sha256Bytes(Files.readAllBytes(maskPath)))) {
                issues.add("render:mask hash " + variantId);
            }
            if (!property(render, "render_metadata_sha256").equals(JsonCanonical.sha256(metadata))) {
                issues.add("render:metadata hash " + variantId);
            }
        }

        private static void validatePng(Path path, List<String> issues) throws IOException {
            try (InputStream input = Files.newInputStream(path);
                 NativeImage image = NativeImage.read(input)) {
                if (image.getWidth() != ExporterConstants.IMAGE_SIZE
                    || image.getHeight() != ExporterConstants.IMAGE_SIZE) {
                    issues.add("render:PNG dimensions " + path);
                }
            } catch (IOException | RuntimeException exception) {
                issues.add("render:PNG decode " + path + ": " + safeMessage(exception));
            }
        }

        private static void checkFailureReference(
            JsonObject failure,
            Map<String, JsonObject> blocks,
            Map<String, JsonObject> states,
            Map<String, JsonObject> variants,
            List<String> issues
        ) {
            String scope = property(failure, "scope");
            if ("block".equals(scope) && !blocks.containsKey(property(failure, "block_id"))) {
                issues.add("failure:block reference");
            }
            if ("state".equals(scope) && (!blocks.containsKey(property(failure, "block_id"))
                || !states.containsKey(property(failure, "state_id")))) {
                issues.add("failure:state reference");
            }
            if (("variant".equals(scope) || "render".equals(scope))
                && !variants.containsKey(property(failure, "variant_id"))) {
                issues.add("failure:variant reference");
            }
        }

        private static String property(JsonObject object, String name) {
            return object.has(name) && object.get(name).isJsonPrimitive() ? object.get(name).getAsString() : "";
        }

        private static void checkCount(JsonObject counts, String name, long expected, List<String> issues) {
            if (!counts.has(name) || counts.get(name).getAsLong() != expected) {
                issues.add("manifest:count " + name);
            }
        }
    }

    private static String summarizeIssues(List<String> issues) {
        int shown = Math.min(8, issues.size());
        StringBuilder summary = new StringBuilder("issues=").append(issues.size());
        if (shown > 0) {
            summary.append(": ");
            for (int index = 0; index < shown; index++) {
                if (index > 0) {
                    summary.append("; ");
                }
                summary.append(issues.get(index));
            }
            if (shown < issues.size()) {
                summary.append("; ...");
            }
        }
        return truncateMessage(summary.toString());
    }

    private static String truncateMessage(String message) {
        if (message.codePointCount(0, message.length()) <= 500) {
            return message;
        }
        return message.substring(0, message.offsetByCodePoints(0, 500));
    }

    private static String atomicMoveFailureDetail(IOException exception) {
        StringBuilder detail = new StringBuilder(exception.getClass().getName());
        if (exception instanceof FileSystemException fileSystemException) {
            if (fileSystemException.getFile() != null) {
                detail.append(" file=").append(fileSystemException.getFile());
            }
            if (fileSystemException.getOtherFile() != null) {
                detail.append(" otherFile=").append(fileSystemException.getOtherFile());
            }
            if (fileSystemException.getReason() != null) {
                detail.append(" reason=").append(fileSystemException.getReason());
            }
        }
        if (exception.getMessage() != null && !exception.getMessage().isBlank()) {
            detail.append(": ").append(exception.getMessage());
        }
        return truncateMessage(detail.toString());
    }

    private void appendLog(String message) {
        try {
            Files.writeString(
                stagingDirectory.resolve("exporter.log"),
                message + "\n",
                StandardCharsets.UTF_8,
                StandardOpenOption.CREATE,
                StandardOpenOption.APPEND,
                StandardOpenOption.WRITE
            );
        } catch (IOException exception) {
            failures.add(ExportFailure.exportFailure(
                exportId,
                exporterVersion,
                "IO_ERROR",
                "cannot append exporter.log: " + safeMessage(exception),
                logicalInputSignature,
                Instant.now()
            ));
        }
    }

    private static String reasonCode(Exception exception) {
        if (exception instanceof RenderExporter.RenderValidationException renderValidation) {
            return renderValidation.reasonCode();
        }
        if (exception instanceof IOException) {
            return "IO_ERROR";
        }
        return "EXPORTER_EXCEPTION";
    }

    private static String safeMessage(Throwable throwable) {
        String message = throwable.getMessage();
        return message == null || message.isBlank() ? throwable.getClass().getSimpleName() : message;
    }

}
