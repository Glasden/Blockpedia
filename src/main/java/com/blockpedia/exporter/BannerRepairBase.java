package com.blockpedia.exporter;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import net.minecraft.resources.Identifier;

import java.io.IOException;
import java.nio.file.attribute.BasicFileAttributes;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/** Validates and supplies the one complete base export consumed by banner-repair. */
final class BannerRepairBase {
    private static final Pattern CHECKSUM_LINE = Pattern.compile("^([0-9a-f]{64})  (.+)$");
    private static final Set<String> REQUIRED_TOP_LEVEL = Set.of(
        "manifest.json", "blocks.jsonl", "states.jsonl", "variants.jsonl",
        "failures.jsonl", "checksums.sha256", "exporter.log", "renders"
    );

    final String exportId;
    final Path directory;
    final JsonObject manifest;
    final List<JsonObject> blocks;
    final List<JsonObject> states;
    final List<JsonObject> variants;
    final List<JsonObject> failures;

    private BannerRepairBase(
        String exportId,
        Path directory,
        JsonObject manifest,
        List<JsonObject> blocks,
        List<JsonObject> states,
        List<JsonObject> variants,
        List<JsonObject> failures
    ) {
        this.exportId = exportId;
        this.directory = directory;
        this.manifest = manifest;
        this.blocks = blocks;
        this.states = states;
        this.variants = variants;
        this.failures = failures;
    }

    static BannerRepairBase verify(
        Path exportParent,
        String baseExportId,
        List<Identifier> runtimeRegistry,
        String currentResourceHash
    ) throws IOException {
        if (!ExportIdentity.isValidExportId(baseExportId)) {
            throw new IOException("invalid base export ID: " + baseExportId);
        }
        Path normalizedParent = exportParent.toAbsolutePath().normalize();
        Path directory = normalizedParent.resolve(baseExportId).normalize();
        if (!directory.startsWith(normalizedParent) || !directory.getFileName().toString().equals(baseExportId)) {
            throw new IOException("base export path escapes version export root");
        }
        rejectLinks(normalizedParent);
        rejectLinks(directory);
        if (directory.getFileName().toString().startsWith(".")
            || !Files.isDirectory(directory, LinkOption.NOFOLLOW_LINKS)) {
            throw new IOException("base export is not a final directory: " + baseExportId);
        }
        for (String required : REQUIRED_TOP_LEVEL) {
            Path path = directory.resolve(required);
            if ("renders".equals(required)) {
                if (!Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS)) {
                    throw new IOException("base export is missing renders/: " + baseExportId);
                }
            } else if (!Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)) {
                throw new IOException("base export is missing " + required + ": " + baseExportId);
            }
        }

        JsonObject manifest = readObject(directory.resolve("manifest.json"), "manifest.json");
        validateManifest(manifest, baseExportId, runtimeRegistry, currentResourceHash);
        verifyChecksums(directory);

        List<JsonObject> blocks = readJsonl(directory.resolve("blocks.jsonl"));
        List<JsonObject> states = readJsonl(directory.resolve("states.jsonl"));
        List<JsonObject> variants = readJsonl(directory.resolve("variants.jsonl"));
        List<JsonObject> failures = readJsonl(directory.resolve("failures.jsonl"));
        List<String> commitIssues = ExportPackage.CommitValidator.validate(directory, manifest, runtimeRegistry);
        if (!commitIssues.isEmpty()) {
            throw new IOException("base export commit validation failed: " + summarize(commitIssues));
        }
        verifyBannerTargets(directory, variants, failures);
        return new BannerRepairBase(baseExportId, directory, manifest, blocks, states, variants, failures);
    }

    void copyNonTargetRenders(Path replacementDirectory) throws IOException {
        for (JsonObject variant : variants) {
            String variantId = property(variant, "variant_id");
            if (!"selected".equals(property(variant, "status"))
                || ExporterConstants.isBannerRepairTarget(variantId)) {
                continue;
            }
            RenderPaths.Location location = RenderPaths.forBlockId(variantId);
            Path sourceDirectory = location.directory(directory);
            rejectLinks(sourceDirectory);
            for (String fileName : List.of("preview.png", "mask.png", "render.json")) {
                Path source = sourceDirectory.resolve(fileName);
                if (!Files.isRegularFile(source, LinkOption.NOFOLLOW_LINKS)) {
                    throw new IOException("base selected render artifact is missing: " + source);
                }
                Path destination = location.directory(replacementDirectory).resolve(fileName);
                Files.createDirectories(destination.getParent());
                Files.copy(source, destination, StandardCopyOption.COPY_ATTRIBUTES);
            }
        }
    }

    List<JsonObject> rebasedBlocks(String newExportId, Instant timestamp) {
        return rebaseAll(blocks, newExportId, timestamp, null);
    }

    List<JsonObject> rebasedStates(String newExportId, Instant timestamp) {
        return rebaseAll(states, newExportId, timestamp, null);
    }

    List<JsonObject> rebasedVariants(String newExportId, Instant timestamp) {
        return rebaseAll(variants, newExportId, timestamp, null);
    }

    List<JsonObject> rebasedFailures(
        String newExportId,
        String renderInputSignature,
        Instant timestamp
    ) {
        List<JsonObject> result = new ArrayList<>();
        for (JsonObject failure : failures) {
            if (ExporterConstants.isBannerRepairTarget(property(failure, "variant_id"))) {
                continue;
            }
            result.add(rebase(failure, newExportId, renderInputSignature, timestamp));
        }
        return result;
    }

    JsonObject selectedBannerVariant(
        String targetId,
        String newExportId,
        String renderInputSignature,
        Instant timestamp,
        JsonObject renderReference
    ) throws IOException {
        JsonObject baseVariant = findVariant(targetId);
        JsonObject block = findById(blocks, "block_id", targetId);
        List<JsonObject> blockStates = states.stream()
            .filter(state -> targetId.equals(property(state, "block_id")))
            .sorted(Comparator.comparing(state -> property(state, "state_id"), JsonCanonical.utf8Comparator()))
            .toList();
        if (block == null || blockStates.isEmpty()) {
            throw new IOException("base target records are incomplete: " + targetId);
        }
        String defaultStateId = property(block, "default_state_id");
        if (defaultStateId.isEmpty()) {
            throw new IOException("base target default state is missing: " + targetId);
        }
        JsonObject result = rebase(baseVariant, newExportId, renderInputSignature, timestamp);
        result.addProperty("status", "selected");
        result.addProperty("candidate_qualification", "eligible");
        result.remove("skip_reason_code");
        result.remove("skip_reason");
        result.addProperty("canonical_state_id", defaultStateId);
        JsonArray representedStateIds = new JsonArray();
        for (JsonObject state : blockStates) {
            representedStateIds.add(property(state, "state_id"));
        }
        result.add("represented_state_ids", representedStateIds);
        result.add("context", contextJson());
        result.add("selection", selectionJson(defaultStateId, representedStateIds));
        result.add("machine_facts", machineFacts(blockStates, defaultStateId));
        result.add("render", renderReference);
        return result;
    }

    private static List<JsonObject> rebaseAll(
        List<JsonObject> source,
        String newExportId,
        Instant timestamp,
        String inputSignature
    ) {
        return source.stream()
            .map(item -> rebase(item, newExportId, inputSignature, timestamp))
            .toList();
    }

    private static JsonObject rebase(
        JsonObject source,
        String newExportId,
        String inputSignature,
        Instant timestamp
    ) {
        JsonObject result = copyObject(source);
        rewrite(result, newExportId, inputSignature, timestamp);
        return result;
    }

    private static void rewrite(JsonElement element, String newExportId, String inputSignature, Instant timestamp) {
        if (element.isJsonObject()) {
            JsonObject object = element.getAsJsonObject();
            for (String key : new ArrayList<>(object.keySet())) {
                JsonElement value = object.get(key);
                if ("export_id".equals(key)) {
                    object.addProperty(key, newExportId);
                } else if ("exporter_version".equals(key) || "producer_version".equals(key)) {
                    object.addProperty(key, ExporterConstants.EXPORTER_VERSION);
                } else if ("input_signature".equals(key) && inputSignature != null) {
                    object.addProperty(key, inputSignature);
                } else if ("created_at".equals(key) && timestamp != null) {
                    object.addProperty(key, JsonCanonical.timestamp(timestamp));
                } else {
                    rewrite(value, newExportId, inputSignature, timestamp);
                }
            }
        } else if (element.isJsonArray()) {
            for (JsonElement item : element.getAsJsonArray()) {
                rewrite(item, newExportId, inputSignature, timestamp);
            }
        }
    }

    private static void validateManifest(
        JsonObject manifest,
        String exportId,
        List<Identifier> runtimeRegistry,
        String currentResourceHash
    ) throws IOException {
        require(manifest, "schema_version", "export-manifest.v1");
        require(manifest, "export_contract_version", ExporterConstants.EXPORT_CONTRACT_VERSION);
        require(manifest, "export_id", exportId);
        String status = property(manifest, "status");
        if (!"succeeded".equals(status) && !"needs_review".equals(status)) {
            throw new IOException("base export is not complete: status=" + status);
        }
        JsonObject toolchain = requiredObject(manifest, "toolchain");
        require(toolchain, "minecraft_version", ExporterConstants.MINECRAFT_VERSION);
        require(toolchain, "java_version", "25");
        require(toolchain, "fabric_loader_version", "0.19.3");
        require(toolchain, "fabric_api_version", "0.157.0+26.2");
        require(toolchain, "loom_version", "1.17.19");
        require(toolchain, "gradle_version", "9.5.1");
        require(toolchain, "mappings", "Minecraft 26.2 native Mojang names (unobfuscated); no external mappings artifact");
        require(toolchain, "exporter_mod_id", ExporterConstants.MOD_ID);
        JsonObject runtime = requiredObject(manifest, "runtime");
        require(runtime, "resource_pack_id", "vanilla");
        require(runtime, "resource_pack_sha256", currentResourceHash);
        JsonObject renderEnvironment = requiredObject(manifest, "render_environment");
        require(renderEnvironment, "camera_policy_version", "camera.v1");
        JsonObject scope = requiredObject(manifest, "scope");
        require(scope, "namespace", "minecraft");
        require(scope, "registry", "block");
        String registryHash = JsonCanonical.sha256String(
            String.join("\n", runtimeRegistry.stream().map(Identifier::toString).toList())
        );
        if (!registryHash.equals(property(scope, "registry_snapshot_sha256"))) {
            throw new IOException("base export registry snapshot does not match runtime");
        }
        JsonObject policies = requiredObject(manifest, "policies");
        require(policies, "render_policy_version", ExporterConstants.RENDER_POLICY_VERSION);
        String expectedLogicalSignature = JsonCanonical.sha256Framed(
            ExporterConstants.MINECRAFT_VERSION,
            ExporterConstants.EXPORT_CONTRACT_VERSION,
            ExporterConstants.STATE_POLICY_VERSION,
            ExporterConstants.RENDER_POLICY_VERSION,
            ExporterConstants.FIXTURE_POLICY_VERSION,
            ExporterConstants.DEDUPE_POLICY_VERSION,
            ExporterConstants.PRE_RENDER_SKIP_POLICY_TOKEN,
            currentResourceHash,
            registryHash
        );
        if (!expectedLogicalSignature.equals(property(manifest, "logical_input_signature"))) {
            throw new IOException("base export logical input signature does not match historical D-043 inputs");
        }
        JsonObject counts = requiredObject(manifest, "counts");
        if (counts.get("registry_blocks") == null || counts.get("registry_blocks").getAsInt() != runtimeRegistry.size()) {
            throw new IOException("base export registry count does not match runtime");
        }
    }

    private static void verifyBannerTargets(Path directory, List<JsonObject> variants, List<JsonObject> failures) throws IOException {
        Map<String, JsonObject> variantsById = index(variants, "variant_id");
        Map<String, List<JsonObject>> failuresById = new HashMap<>();
        for (JsonObject failure : failures) {
            String variantId = property(failure, "variant_id");
            if (!variantId.isEmpty()) {
                failuresById.computeIfAbsent(variantId, ignored -> new ArrayList<>()).add(failure);
            }
        }
        for (String targetId : ExporterConstants.BANNER_REPAIR_TARGET_IDS) {
            JsonObject variant = variantsById.get(targetId);
            if (variant == null || !"skipped".equals(property(variant, "status"))
                || !"OBJECT_OFF_CANVAS".equals(property(variant, "skip_reason_code"))) {
                throw new IOException("base banner target is not the required skipped OBJECT_OFF_CANVAS variant: " + targetId);
            }
            List<JsonObject> targetFailures = failuresById.getOrDefault(targetId, List.of());
            if (targetFailures.size() != 1
                || !"OBJECT_OFF_CANVAS".equals(property(targetFailures.get(0), "reason_code"))
                || !"skip".equals(property(targetFailures.get(0), "kind"))
                || !"pending".equals(property(targetFailures.get(0), "review_status"))
                || !targetId.equals(property(targetFailures.get(0), "block_id"))
                || !targetId.equals(property(targetFailures.get(0), "logical_key"))) {
                throw new IOException("base banner target failure is not an exact pending OBJECT_OFF_CANVAS skip: " + targetId);
            }
            Path renderDirectory = RenderPaths.forBlockId(targetId).directory(directory);
            if (Files.exists(renderDirectory, LinkOption.NOFOLLOW_LINKS)) {
                throw new IOException("base skipped banner target has render directory: " + targetId);
            }
        }
    }

    private static void verifyChecksums(Path directory) throws IOException {
        Path checksumPath = directory.resolve("checksums.sha256");
        Set<String> declared = new HashSet<>();
        for (String line : Files.readAllLines(checksumPath, StandardCharsets.UTF_8)) {
            if (line.isEmpty()) {
                continue;
            }
            var match = CHECKSUM_LINE.matcher(line);
            if (!match.matches()) {
                throw new IOException("invalid base checksums line");
            }
            String relative = match.group(2);
            if (!safeRelative(relative) || !declared.add(relative) || "checksums.sha256".equals(relative)) {
                throw new IOException("invalid base checksum path: " + relative);
            }
            Path file = directory.resolve(relative).normalize();
            if (!file.startsWith(directory) || !Files.isRegularFile(file, LinkOption.NOFOLLOW_LINKS)) {
                throw new IOException("base checksum path is not a regular file: " + relative);
            }
            String actual = JsonCanonical.sha256Bytes(Files.readAllBytes(file)).substring("sha256:".length());
            if (!actual.equals(match.group(1))) {
                throw new IOException("base checksum mismatch: " + relative);
            }
        }
        Set<String> actualFiles = new HashSet<>();
        Set<Object> fileKeys = new HashSet<>();
        Object checksumFileKey = Files.readAttributes(
            checksumPath,
            BasicFileAttributes.class,
            LinkOption.NOFOLLOW_LINKS
        ).fileKey();
        if (checksumFileKey != null) {
            fileKeys.add(checksumFileKey);
        }
        try (Stream<Path> stream = Files.walk(directory)) {
            for (Path path : stream.toList()) {
                if (Files.isSymbolicLink(path)) {
                    throw new IOException("base export contains a symbolic link: " + path);
                }
                if (Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)
                    && !path.equals(checksumPath)) {
                    actualFiles.add(directory.relativize(path).toString().replace('\\', '/'));
                    Object fileKey = Files.readAttributes(path, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS).fileKey();
                    if (fileKey != null && !fileKeys.add(fileKey)) {
                        throw new IOException("base export contains a hardlink: " + path);
                    }
                }
            }
        }
        if (!actualFiles.equals(declared)) {
            throw new IOException("base checksum file set does not match export files");
        }
    }

    private static void rejectLinks(Path path) throws IOException {
        Path absolute = path.toAbsolutePath().normalize();
        Path current = absolute.getRoot();
        if (current != null) {
            for (Path component : absolute) {
                current = current.resolve(component);
                if (Files.isSymbolicLink(current)) {
                    throw new IOException("base export path contains a symbolic link: " + current);
                }
            }
        }
        if (Files.exists(absolute, LinkOption.NOFOLLOW_LINKS)) {
            try (Stream<Path> stream = Files.walk(absolute)) {
                for (Path child : stream.toList()) {
                    if (Files.isSymbolicLink(child)) {
                        throw new IOException("base export contains a symbolic link: " + child);
                    }
                }
            }
        }
    }

    private static List<JsonObject> readJsonl(Path path) throws IOException {
        byte[] raw = Files.readAllBytes(path);
        if (raw.length > 0 && raw[raw.length - 1] != '\n') {
            throw new IOException("base JSONL has no final LF: " + path.getFileName());
        }
        String text = new String(raw, StandardCharsets.UTF_8);
        if (text.indexOf('\r') >= 0) {
            throw new IOException("base JSONL contains CR: " + path.getFileName());
        }
        List<JsonObject> result = new ArrayList<>();
        for (String line : text.split("\\n", -1)) {
            if (line.isEmpty()) {
                continue;
            }
            JsonElement parsed;
            try {
                parsed = JsonParser.parseString(line);
            } catch (RuntimeException exception) {
                throw new IOException("invalid base JSONL: " + path.getFileName(), exception);
            }
            if (!parsed.isJsonObject()) {
                throw new IOException("base JSONL line is not an object: " + path.getFileName());
            }
            result.add(parsed.getAsJsonObject());
        }
        return List.copyOf(result);
    }

    private static JsonObject readObject(Path path, String label) throws IOException {
        try {
            JsonElement parsed = JsonParser.parseString(Files.readString(path, StandardCharsets.UTF_8));
            if (!parsed.isJsonObject()) {
                throw new IOException(label + " is not an object");
            }
            return parsed.getAsJsonObject();
        } catch (RuntimeException exception) {
            throw new IOException("invalid base " + label, exception);
        }
    }

    private static JsonObject machineFacts(List<JsonObject> states, String defaultStateId) throws IOException {
        JsonObject defaultState = findRequired(states, "state_id", defaultStateId);
        JsonObject result = new JsonObject();
        JsonObject shape = copyObject(defaultState.getAsJsonObject("shape"));
        JsonObject collision = copyObject(defaultState.getAsJsonObject("collision"));
        JsonObject behavior = copyObject(defaultState.getAsJsonObject("behavior"));
        result.addProperty("geometry_signature", property(shape, "signature"));
        result.addProperty("collision_signature", property(collision, "signature"));
        result.add("shape", shape);
        result.add("collision", collision);
        result.addProperty("behavior_fingerprint", JsonCanonical.sha256(behavior));
        JsonObject behaviorByState = new JsonObject();
        for (JsonObject state : states) {
            behaviorByState.add(property(state, "state_id"), copyObject(state.getAsJsonObject("behavior")));
        }
        result.add("behavior_by_state", behaviorByState);
        return result;
    }

    private static JsonObject contextJson() {
        JsonObject context = new JsonObject();
        context.addProperty("fixture_id", ExporterConstants.FIXTURE_ID);
        context.addProperty("fixture_version", ExporterConstants.FIXTURE_POLICY_VERSION);
        context.addProperty("rotatable", false);
        context.add("canonical_orientation", com.google.gson.JsonNull.INSTANCE);
        context.add("adjacency", new JsonArray());
        return context;
    }

    private static JsonObject selectionJson(String canonicalStateId, JsonArray representedStateIds) {
        JsonObject selection = new JsonObject();
        selection.addProperty("state_policy_version", ExporterConstants.STATE_POLICY_VERSION);
        selection.addProperty("reason", "default_state");
        selection.add("protected_dimensions", new JsonArray());
        JsonArray folded = new JsonArray();
        for (JsonElement stateId : representedStateIds) {
            if (!canonicalStateId.equals(stateId.getAsString())) {
                folded.add(stateId.getAsString());
            }
        }
        selection.add("folded_state_ids", folded);
        selection.add("policy_override_id", com.google.gson.JsonNull.INSTANCE);
        return selection;
    }

    private static JsonObject copyObject(JsonElement element) {
        return JsonParser.parseString(JsonCanonical.canonical(element)).getAsJsonObject();
    }

    private static Map<String, JsonObject> index(List<JsonObject> objects, String key) throws IOException {
        Map<String, JsonObject> result = new HashMap<>();
        for (JsonObject object : objects) {
            String value = property(object, key);
            if (value.isEmpty() || result.put(value, object) != null) {
                throw new IOException("duplicate or missing base record key: " + key);
            }
        }
        return result;
    }

    private JsonObject findVariant(String targetId) throws IOException {
        return findRequired(variants, "variant_id", targetId);
    }

    private static JsonObject findById(List<JsonObject> objects, String key, String value) {
        return objects.stream().filter(object -> value.equals(property(object, key))).findFirst().orElse(null);
    }

    private static JsonObject findRequired(List<JsonObject> objects, String key, String value) throws IOException {
        JsonObject result = findById(objects, key, value);
        if (result == null) {
            throw new IOException("missing base record " + key + "=" + value);
        }
        return result;
    }

    private static JsonObject requiredObject(JsonObject object, String key) throws IOException {
        JsonElement value = object.get(key);
        if (value == null || !value.isJsonObject()) {
            throw new IOException("base manifest object is missing: " + key);
        }
        return value.getAsJsonObject();
    }

    private static void require(JsonObject object, String key, String expected) throws IOException {
        if (!expected.equals(property(object, key))) {
            throw new IOException("base manifest " + key + " mismatch");
        }
    }

    private static String property(JsonObject object, String key) {
        return object.has(key) && object.get(key).isJsonPrimitive() ? object.get(key).getAsString() : "";
    }

    private static boolean safeRelative(String value) {
        if (value.isEmpty() || value.indexOf('\\') >= 0 || value.indexOf('\u0000') >= 0
            || value.startsWith("/") || value.startsWith("\\")
            || (value.length() >= 2 && Character.isLetter(value.charAt(0)) && value.charAt(1) == ':')) {
            return false;
        }
        for (String segment : value.split("/", -1)) {
            if (segment.isEmpty() || ".".equals(segment) || "..".equals(segment)) {
                return false;
            }
        }
        return true;
    }

    private static String summarize(List<String> issues) {
        int shown = Math.min(8, issues.size());
        StringBuilder result = new StringBuilder("issues=").append(issues.size());
        for (int index = 0; index < shown; index++) {
            result.append(index == 0 ? ": " : "; ").append(issues.get(index));
        }
        return result.toString();
    }
}
