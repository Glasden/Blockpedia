package com.blockpedia.exporter;

import net.minecraft.client.Minecraft;
import net.minecraft.client.resources.language.ClientLanguage;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.Identifier;
import net.minecraft.world.item.Items;
import net.minecraft.world.level.BlockGetter;
import net.minecraft.world.level.EmptyBlockGetter;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.StateDefinition;
import net.minecraft.world.level.block.state.properties.Property;
import net.minecraft.world.phys.AABB;
import net.minecraft.world.phys.shapes.CollisionContext;
import net.minecraft.world.phys.shapes.VoxelShape;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

final class ExportRecords {
    private static final BlockGetter ISOLATED_BLOCK_GETTER = EmptyBlockGetter.INSTANCE;
    private static final BlockPos ISOLATED_POS = BlockPos.ZERO;
    private static final CollisionContext ISOLATED_COLLISION = CollisionContext.empty();

    private ExportRecords() {
    }

    static List<Identifier> minecraftBlockIds() {
        List<Identifier> ids = new ArrayList<>();
        for (Identifier identifier : BuiltInRegistries.BLOCK.keySet()) {
            if ("minecraft".equals(identifier.getNamespace())) {
                ids.add(identifier);
            }
        }
        ids.sort(Comparator.comparing(Identifier::toString, JsonCanonical.utf8Comparator()));
        return ids;
    }

    static BlockData collectBlock(Minecraft minecraft, String exportId, String exporterVersion, Identifier id) {
        Block block = BuiltInRegistries.BLOCK.getValue(id);
        if (block == null) {
            throw new IllegalStateException("missing runtime block for " + id);
        }
        StateDefinition<Block, BlockState> definition = block.getStateDefinition();
        List<BlockState> legalStates = new ArrayList<>(definition.getPossibleStates());
        Map<BlockState, String> stateIds = new HashMap<>();
        for (BlockState state : legalStates) {
            stateIds.put(state, canonicalStateId(id.toString(), state));
        }
        legalStates.sort(Comparator.comparing(state -> stateIds.get(state), JsonCanonical.utf8Comparator()));
        String defaultStateId = stateIds.get(block.defaultBlockState());
        if (defaultStateId == null) {
            throw new IllegalStateException("runtime default state is not legal: " + id);
        }

        ClientLanguage zh = ClientLanguage.loadFrom(
            minecraft.getResourceManager(),
            List.of("zh_cn"),
            false
        );
        ClientLanguage en = ClientLanguage.loadFrom(
            minecraft.getResourceManager(),
            List.of("en_us"),
            false
        );
        String descriptionId = block.getDescriptionId();
        String nameZh = zh.has(descriptionId) ? zh.getOrDefault(descriptionId, "") : null;
        String nameEn = en.has(descriptionId) ? en.getOrDefault(descriptionId, "") : null;

        JsonObject blockJson = new JsonObject();
        blockJson.addProperty("schema_version", "export-block.v1");
        blockJson.addProperty("export_id", exportId);
        blockJson.addProperty("minecraft_version", ExporterConstants.MINECRAFT_VERSION);
        blockJson.addProperty("block_id", id.toString());
        blockJson.addProperty("translation_key", descriptionId);
        if (nameZh == null) {
            blockJson.add("name_zh_cn", com.google.gson.JsonNull.INSTANCE);
        } else {
            blockJson.addProperty("name_zh_cn", nameZh);
        }
        if (nameEn == null) {
            blockJson.add("name_en_us", com.google.gson.JsonNull.INSTANCE);
        } else {
            blockJson.addProperty("name_en_us", nameEn);
        }
        blockJson.addProperty("default_state_id", defaultStateId);
        blockJson.add("properties", propertyDefinitions(definition));
        blockJson.addProperty("has_item", block.asItem() != Items.AIR);
        blockJson.addProperty("has_block_entity", block.defaultBlockState().hasBlockEntity());
        blockJson.add("tags", tags(block));
        blockJson.add("behavior", behavior(block.defaultBlockState()));
        blockJson.add("source", runtimeSource(exportId, exporterVersion));

        List<StateData> states = new ArrayList<>();
        for (BlockState state : legalStates) {
            String stateId = stateIds.get(state);
            states.add(new StateData(
                state,
                stateId,
                state.equals(block.defaultBlockState()),
                stateProperties(state),
                shapeFacts(state.getShape(ISOLATED_BLOCK_GETTER, ISOLATED_POS)),
                shapeFacts(state.getCollisionShape(ISOLATED_BLOCK_GETTER, ISOLATED_POS, ISOLATED_COLLISION)),
                behavior(state),
                null,
                "skipped",
                exportId,
                exporterVersion,
                id.toString()
            ));
        }
        return new BlockData(
            block,
            id,
            exportId,
            exporterVersion,
            defaultStateId,
            legalStates,
            states,
            blockJson
        );
    }

    static String canonicalStateId(String blockId, BlockState state) {
        List<Property<?>> properties = new ArrayList<>(state.getProperties());
        properties.sort(Comparator.comparing(Property::getName, JsonCanonical.utf8Comparator()));
        if (properties.isEmpty()) {
            return blockId;
        }
        StringBuilder result = new StringBuilder(blockId).append('[');
        for (int index = 0; index < properties.size(); index++) {
            if (index > 0) {
                result.append(',');
            }
            Property<?> property = properties.get(index);
            result.append(property.getName()).append('=');
            result.append(propertyValueName(property, state));
        }
        return result.append(']').toString();
    }

    private static JsonObject propertyDefinitions(StateDefinition<Block, BlockState> definition) {
        JsonObject result = new JsonObject();
        List<Property<?>> properties = new ArrayList<>(definition.getProperties());
        properties.sort(Comparator.comparing(Property::getName, JsonCanonical.utf8Comparator()));
        for (Property<?> property : properties) {
            List<String> values = new ArrayList<>();
            for (Object value : property.getPossibleValues()) {
                values.add(propertyValueName(property, value));
            }
            values.sort(JsonCanonical.utf8Comparator());
            result.add(property.getName(), JsonCanonical.GSON.toJsonTree(values));
        }
        return result;
    }

    private static JsonObject stateProperties(BlockState state) {
        JsonObject result = new JsonObject();
        List<Property<?>> properties = new ArrayList<>(state.getProperties());
        properties.sort(Comparator.comparing(Property::getName, JsonCanonical.utf8Comparator()));
        for (Property<?> property : properties) {
            result.addProperty(property.getName(), propertyValueName(property, state));
        }
        return result;
    }

    @SuppressWarnings({"unchecked", "rawtypes"})
    private static String propertyValueName(Property property, Object stateOrValue) {
        Object value = stateOrValue instanceof BlockState
            ? ((BlockState) stateOrValue).getValue(property)
            : stateOrValue;
        return property.getName((Comparable) value);
    }

    private static JsonArray tags(Block block) {
        List<String> values = block.builtInRegistryHolder().tags()
            .filter(tag -> "minecraft".equals(tag.location().getNamespace()))
            .map(tag -> tag.location().toString())
            .sorted(JsonCanonical.utf8Comparator())
            .toList();
        JsonArray result = new JsonArray();
        values.forEach(result::add);
        return result;
    }

    static JsonObject behavior(BlockState state) {
        JsonObject result = new JsonObject();
        result.addProperty("transparent", !state.canOcclude());
        result.addProperty("emissive", state.emissiveRendering());
        result.addProperty("passable", !state.blocksMotion());
        result.addProperty("waterloggable", state.getBlock().getStateDefinition().getProperty("waterlogged") != null);
        result.addProperty("redstone_related", state.isSignalSource() || state.hasAnalogOutputSignal());
        result.addProperty("requires_support", "unknown");
        result.add("support", unknownSupport());
        result.addProperty("emission_level", state.getLightEmission());
        return result;
    }

    private static JsonObject unknownSupport() {
        JsonObject support = new JsonObject();
        for (String direction : List.of("above", "below", "east", "north", "south", "west", "none")) {
            support.addProperty(direction, "unknown");
        }
        return support;
    }

    static JsonObject shapeFacts(VoxelShape shape) {
        List<AABB> boxes = new ArrayList<>(shape.toAabbs());
        boxes.sort(Comparator
            .comparingDouble((AABB box) -> box.minX)
            .thenComparingDouble(box -> box.minY)
            .thenComparingDouble(box -> box.minZ)
            .thenComparingDouble(box -> box.maxX)
            .thenComparingDouble(box -> box.maxY)
            .thenComparingDouble(box -> box.maxZ));
        JsonArray boxArray = new JsonArray();
        for (AABB box : boxes) {
            JsonObject item = new JsonObject();
            item.addProperty("min_x", box.minX);
            item.addProperty("min_y", box.minY);
            item.addProperty("min_z", box.minZ);
            item.addProperty("max_x", box.maxX);
            item.addProperty("max_y", box.maxY);
            item.addProperty("max_z", box.maxZ);
            boxArray.add(item);
        }
        JsonObject result = new JsonObject();
        result.add("boxes", boxArray);
        result.addProperty("signature", JsonCanonical.sha256(boxArray));
        return result;
    }

    static JsonObject runtimeSource(String exportId, String exporterVersion) {
        JsonObject source = new JsonObject();
        source.addProperty("type", "runtime");
        source.addProperty("minecraft_version", ExporterConstants.MINECRAFT_VERSION);
        source.addProperty("export_id", exportId);
        source.addProperty("exporter_version", exporterVersion);
        source.addProperty("stage", "EXPORT_REGISTRY");
        return source;
    }

    static JsonObject machineSource(String exportId, String exporterVersion) {
        JsonObject source = new JsonObject();
        source.addProperty("type", "machine");
        source.addProperty("minecraft_version", ExporterConstants.MINECRAFT_VERSION);
        source.addProperty("export_id", exportId);
        source.addProperty("producer_version", exporterVersion);
        source.addProperty("stage", "RENDER_VARIANTS");
        return source;
    }

    static final class BlockData {
        final Block block;
        final Identifier blockId;
        final String exportId;
        final String exporterVersion;
        final String defaultStateId;
        final List<BlockState> legalStates;
        final List<StateData> states;
        final JsonObject blockJson;
        VariantData variant;

        BlockData(
            Block block,
            Identifier blockId,
            String exportId,
            String exporterVersion,
            String defaultStateId,
            List<BlockState> legalStates,
            List<StateData> states,
            JsonObject blockJson
        ) {
            this.block = block;
            this.blockId = blockId;
            this.exportId = exportId;
            this.exporterVersion = exporterVersion;
            this.defaultStateId = defaultStateId;
            this.legalStates = legalStates;
            this.states = states;
            this.blockJson = blockJson;
        }
    }

    static final class StateData {
        final BlockState state;
        final String stateId;
        final boolean isDefault;
        final JsonObject properties;
        final JsonObject shape;
        final JsonObject collision;
        final JsonObject behavior;
        String variantId;
        String mappingStatus;
        final String exportId;
        final String exporterVersion;
        final String blockId;

        StateData(
            BlockState state,
            String stateId,
            boolean isDefault,
            JsonObject properties,
            JsonObject shape,
            JsonObject collision,
            JsonObject behavior,
            String variantId,
            String mappingStatus,
            String exportId,
            String exporterVersion,
            String blockId
        ) {
            this.state = state;
            this.stateId = stateId;
            this.isDefault = isDefault;
            this.properties = properties;
            this.shape = shape;
            this.collision = collision;
            this.behavior = behavior;
            this.variantId = variantId;
            this.mappingStatus = mappingStatus;
            this.exportId = exportId;
            this.exporterVersion = exporterVersion;
            this.blockId = blockId;
        }

        JsonObject toJson() {
            JsonObject result = new JsonObject();
            result.addProperty("schema_version", "export-state.v1");
            result.addProperty("export_id", exportId);
            result.addProperty("minecraft_version", ExporterConstants.MINECRAFT_VERSION);
            result.addProperty("state_id", stateId);
            result.addProperty("block_id", blockId);
            result.add("properties", properties);
            result.addProperty("is_default", isDefault);
            result.addProperty("legal_state", true);
            result.add("shape", shape);
            result.add("collision", collision);
            result.add("behavior", behavior);
            JsonArray variantIds = new JsonArray();
            if (variantId != null) {
                variantIds.add(variantId);
            }
            result.add("variant_ids", variantIds);
            result.addProperty("mapping_status", mappingStatus);
            result.add("source", runtimeSource(exportId, exporterVersion));
            return result;
        }
    }

    static final class VariantData {
        final String variantId;
        final String blockId;
        final String canonicalStateId;
        final List<String> representedStateIds;
        JsonObject render;
        JsonObject machineFacts;
        String status;
        String skipReasonCode;
        String skipReason;

        VariantData(String variantId, String blockId, String canonicalStateId, List<String> representedStateIds) {
            this.variantId = variantId;
            this.blockId = blockId;
            this.canonicalStateId = canonicalStateId;
            this.representedStateIds = representedStateIds;
        }

        JsonObject toJson(String exportId, String exporterVersion) {
            JsonObject result = new JsonObject();
            result.addProperty("schema_version", "export-variant.v1");
            result.addProperty("export_id", exportId);
            result.addProperty("minecraft_version", ExporterConstants.MINECRAFT_VERSION);
            result.addProperty("variant_id", variantId);
            result.addProperty("block_id", blockId);
            result.addProperty("status", status);
            result.addProperty("candidate_qualification", "selected".equals(status) ? "eligible" : "excluded");
            result.add("warnings", new JsonArray());
            if ("selected".equals(status)) {
                result.addProperty("canonical_state_id", canonicalStateId);
                result.add("represented_state_ids", JsonCanonical.GSON.toJsonTree(representedStateIds));
                result.add("context", context());
                result.add("selection", selection());
                result.add("machine_facts", machineFacts);
                result.add("render", render);
            } else {
                result.addProperty("skip_reason_code", skipReasonCode);
                result.addProperty("skip_reason", skipReason);
            }
            result.add("source", ExportRecords.machineSource(exportId, exporterVersion));
            return result;
        }

        private JsonObject context() {
            JsonObject context = new JsonObject();
            context.addProperty("fixture_id", ExporterConstants.FIXTURE_ID);
            context.addProperty("fixture_version", ExporterConstants.FIXTURE_POLICY_VERSION);
            context.addProperty("rotatable", false);
            context.add("canonical_orientation", com.google.gson.JsonNull.INSTANCE);
            context.add("adjacency", new JsonArray());
            return context;
        }

        private JsonObject selection() {
            JsonObject selection = new JsonObject();
            selection.addProperty("state_policy_version", ExporterConstants.STATE_POLICY_VERSION);
            selection.addProperty("reason", "default_state");
            selection.add("protected_dimensions", new JsonArray());
            List<String> folded = representedStateIds.stream()
                .filter(stateId -> !Objects.equals(stateId, canonicalStateId))
                .sorted(JsonCanonical.utf8Comparator())
                .toList();
            selection.add("folded_state_ids", JsonCanonical.GSON.toJsonTree(folded));
            selection.add("policy_override_id", com.google.gson.JsonNull.INSTANCE);
            return selection;
        }
    }
}
