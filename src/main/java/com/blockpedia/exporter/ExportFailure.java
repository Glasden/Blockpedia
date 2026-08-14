package com.blockpedia.exporter;

import com.google.gson.JsonObject;

import java.time.Instant;
import java.util.List;

final class ExportFailure {
    private final String exportId;
    private final String exporterVersion;
    private final String failureId;
    private final String minecraftVersion;
    private final String kind;
    private final String stage;
    private final String scope;
    private final String blockId;
    private final String stateId;
    private final String variantId;
    private final String logicalKey;
    private final String reasonCode;
    private final String severity;
    private final int retryCount;
    private final String action;
    private final String reviewStatus;
    private final String message;
    private final String inputSignature;
    private final String createdAt;

    private ExportFailure(
        String exportId,
        String exporterVersion,
        String failureId,
        String kind,
        String stage,
        String scope,
        String blockId,
        String stateId,
        String variantId,
        String logicalKey,
        String reasonCode,
        String severity,
        int retryCount,
        String action,
        String reviewStatus,
        String message,
        String inputSignature,
        String createdAt
    ) {
        this.exportId = exportId;
        this.exporterVersion = exporterVersion;
        this.failureId = failureId;
        this.minecraftVersion = ExporterConstants.MINECRAFT_VERSION;
        this.kind = kind;
        this.stage = stage;
        this.scope = scope;
        this.blockId = blockId;
        this.stateId = stateId;
        this.variantId = variantId;
        this.logicalKey = logicalKey;
        this.reasonCode = reasonCode;
        this.severity = severity;
        this.retryCount = retryCount;
        this.action = action;
        this.reviewStatus = reviewStatus;
        this.message = message;
        this.inputSignature = inputSignature;
        this.createdAt = createdAt;
    }

    static ExportFailure skipVariant(
        String exportId,
        String exporterVersion,
        String blockId,
        String variantId,
        String reasonCode,
        String message,
        String inputSignature,
        Instant createdAt
    ) {
        return new ExportFailure(
            exportId,
            exporterVersion,
            "failure_" + JsonCanonical.sha256String(variantId).substring(7, 23),
            "skip",
            "RENDER_VARIANTS",
            "variant",
            blockId,
            null,
            variantId,
            variantId,
            reasonCode,
            "normal",
            0,
            "needs_review",
            "pending",
            message,
            inputSignature,
            JsonCanonical.timestamp(createdAt)
        );
    }

    static ExportFailure exportFailure(
        String exportId,
        String exporterVersion,
        String reasonCode,
        String message,
        String inputSignature,
        Instant createdAt
    ) {
        return new ExportFailure(
            exportId,
            exporterVersion,
            "failure_export_" + JsonCanonical.sha256String(reasonCode).substring(7, 23),
            "failure",
            "EXPORT_REGISTRY",
            "export",
            null,
            null,
            null,
            "export",
            reasonCode,
            "high",
            0,
            "fail_export",
            "not_required",
            message,
            inputSignature,
            JsonCanonical.timestamp(createdAt)
        );
    }

    JsonObject toJson() {
        JsonObject result = new JsonObject();
        result.addProperty("schema_version", "export-failure.v1");
        result.addProperty("export_id", exportId);
        result.addProperty("minecraft_version", minecraftVersion);
        result.addProperty("failure_id", failureId);
        result.addProperty("kind", kind);
        result.addProperty("stage", stage);
        result.addProperty("scope", scope);
        if (blockId != null) {
            result.addProperty("block_id", blockId);
        }
        if (stateId != null) {
            result.addProperty("state_id", stateId);
        }
        if (variantId != null) {
            result.addProperty("variant_id", variantId);
        }
        result.addProperty("logical_key", logicalKey);
        result.addProperty("reason_code", reasonCode);
        result.addProperty("severity", severity);
        result.addProperty("retry_count", retryCount);
        result.addProperty("action", action);
        result.addProperty("review_status", reviewStatus);
        result.addProperty("message", message);
        result.add("evidence", evidence());
        result.addProperty("input_signature", inputSignature);
        result.addProperty("created_at", createdAt);
        return result;
    }

    private JsonObject evidence() {
        JsonObject evidence = new JsonObject();
        evidence.addProperty("kind", "none");
        evidence.add("paths", JsonCanonical.GSON.toJsonTree(List.of()));
        evidence.add("hashes", JsonCanonical.GSON.toJsonTree(List.of()));
        evidence.add("frame_hashes", JsonCanonical.GSON.toJsonTree(List.of()));
        return evidence;
    }
}
