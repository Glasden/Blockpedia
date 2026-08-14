package com.blockpedia.exporter;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonPrimitive;

import java.io.IOException;
import java.io.Writer;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/** RFC 8785 JSON Canonicalization Scheme and SHA-256 helper. */
final class JsonCanonical {
    static final Gson GSON = new GsonBuilder().disableHtmlEscaping().create();
    private static final DateTimeFormatter UTC_SECONDS = DateTimeFormatter
        .ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'")
        .withZone(ZoneOffset.UTC);

    private JsonCanonical() {
    }

    static String timestamp(Instant instant) {
        return UTC_SECONDS.format(instant);
    }

    static String canonical(JsonElement element) {
        if (element == null || element.isJsonNull()) {
            return "null";
        }
        if (element.isJsonObject()) {
            JsonObject object = element.getAsJsonObject();
            List<String> keys = new ArrayList<>(object.keySet());
            keys.sort(JsonCanonical::compareUtf16);
            StringBuilder result = new StringBuilder("{");
            boolean first = true;
            for (String key : keys) {
                if (!first) {
                    result.append(',');
                }
                first = false;
                result.append(quote(key));
                result.append(':');
                result.append(canonical(object.get(key)));
            }
            return result.append('}').toString();
        }
        if (element.isJsonArray()) {
            JsonArray array = element.getAsJsonArray();
            StringBuilder result = new StringBuilder("[");
            for (int index = 0; index < array.size(); index++) {
                if (index > 0) {
                    result.append(',');
                }
                result.append(canonical(array.get(index)));
            }
            return result.append(']').toString();
        }
        JsonPrimitive primitive = element.getAsJsonPrimitive();
        if (primitive.isNumber()) {
            return number(primitive.getAsString());
        }
        if (primitive.isBoolean()) {
            return primitive.getAsBoolean() ? "true" : "false";
        }
        return quote(primitive.getAsString());
    }

    static String sha256(JsonElement element) {
        return sha256Bytes(canonical(element).getBytes(StandardCharsets.UTF_8));
    }

    static String sha256String(String value) {
        return sha256Bytes(value.getBytes(StandardCharsets.UTF_8));
    }

    static String sha256Bytes(byte[] value) {
        return "sha256:" + hex(digest(value));
    }

    static String sha256Framed(String... values) {
        MessageDigest digest = newDigest();
        for (String value : values) {
            if (value != null) {
                digest.update(value.getBytes(StandardCharsets.UTF_8));
            }
            digest.update((byte) 0);
        }
        return "sha256:" + hex(digest.digest());
    }

    static String sha256ResourceSnapshot(Map<String, byte[]> resources) {
        List<String> identifiers = new ArrayList<>(resources.keySet());
        identifiers.sort(JsonCanonical::compareUtf8);
        MessageDigest digest = newDigest();
        for (String identifier : identifiers) {
            byte[] identifierBytes = identifier.getBytes(StandardCharsets.UTF_8);
            byte[] contents = resources.get(identifier);
            digest.update(identifierBytes);
            digest.update((byte) 0);
            byte[] length = ByteBuffer.allocate(Long.BYTES)
                .order(ByteOrder.BIG_ENDIAN)
                .putLong(contents.length)
                .array();
            digest.update(length);
            digest.update(contents);
        }
        return "sha256:" + hex(digest.digest());
    }

    static void writeJson(Path path, JsonElement element) throws IOException {
        Files.writeString(
            path,
            canonical(element) + "\n",
            StandardCharsets.UTF_8,
            StandardOpenOption.CREATE,
            StandardOpenOption.TRUNCATE_EXISTING,
            StandardOpenOption.WRITE
        );
    }

    static void appendJsonLine(Writer writer, JsonElement element) throws IOException {
        writer.write(canonical(element));
        writer.write('\n');
    }

    static int compareUtf8(String left, String right) {
        byte[] leftBytes = left.getBytes(StandardCharsets.UTF_8);
        byte[] rightBytes = right.getBytes(StandardCharsets.UTF_8);
        int length = Math.min(leftBytes.length, rightBytes.length);
        for (int index = 0; index < length; index++) {
            int comparison = Integer.compare(leftBytes[index] & 0xff, rightBytes[index] & 0xff);
            if (comparison != 0) {
                return comparison;
            }
        }
        return Integer.compare(leftBytes.length, rightBytes.length);
    }

    /** RFC 8785 sorts object member names by their UTF-16 code units. */
    static int compareUtf16(String left, String right) {
        int length = Math.min(left.length(), right.length());
        for (int index = 0; index < length; index++) {
            int comparison = Character.compare(left.charAt(index), right.charAt(index));
            if (comparison != 0) {
                return comparison;
            }
        }
        return Integer.compare(left.length(), right.length());
    }

    static Comparator<String> utf8Comparator() {
        return JsonCanonical::compareUtf8;
    }

    /**
     * A small deterministic check for the RFC 8785 boundary values used by R1.
     * It intentionally covers the canonicalizer rather than the exporter data.
     */
    static void selfCheck() {
        JsonObject object = new JsonObject();
        object.addProperty("\ufb33", 1);
        object.addProperty("\ud834\udd1e", 2);
        object.addProperty("a", 3);
        object.addProperty("\r", 4);
        object.addProperty("1", 5);
        String expected = "{\"\\r\":4,\"1\":5,\"a\":3,\"\ud834\udd1e\":2,\"\ufb33\":1}";
        check(expected, canonical(object), "UTF-16 member ordering");
        check("0", canonical(new JsonPrimitive(-0.0d)), "negative zero");
        check("0.000001", canonical(new JsonPrimitive(1e-6d)), "decimal lower boundary");
        check("1e-7", canonical(new JsonPrimitive(1e-7d)), "exponent lower boundary");
        check("100000000000000000000", canonical(new JsonPrimitive(1e20d)), "decimal upper boundary");
        check("1e+21", canonical(new JsonPrimitive(1e21d)), "exponent upper boundary");
        check("5e-324", canonical(new JsonPrimitive(Double.MIN_VALUE)), "smallest non-zero double");
    }

    private static void check(String expected, String actual, String label) {
        if (!Objects.equals(expected, actual)) {
            throw new IllegalStateException("JCS self-check failed for " + label + ": " + actual);
        }
    }

    private static String quote(String value) {
        StringBuilder result = new StringBuilder(value.length() + 2).append('"');
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            switch (character) {
                case '"' -> result.append("\\\"");
                case '\\' -> result.append("\\\\");
                case '\b' -> result.append("\\b");
                case '\f' -> result.append("\\f");
                case '\n' -> result.append("\\n");
                case '\r' -> result.append("\\r");
                case '\t' -> result.append("\\t");
                default -> {
                    if (character < 0x20) {
                        result.append("\\u");
                        result.append(Character.forDigit((character >>> 12) & 0xf, 16));
                        result.append(Character.forDigit((character >>> 8) & 0xf, 16));
                        result.append(Character.forDigit((character >>> 4) & 0xf, 16));
                        result.append(Character.forDigit(character & 0xf, 16));
                    } else if (Character.isHighSurrogate(character)) {
                        if (index + 1 >= value.length() || !Character.isLowSurrogate(value.charAt(index + 1))) {
                            throw new IllegalArgumentException("JCS cannot encode an unpaired high surrogate");
                        }
                        result.append(character).append(value.charAt(++index));
                    } else if (Character.isLowSurrogate(character)) {
                        throw new IllegalArgumentException("JCS cannot encode an unpaired low surrogate");
                    } else {
                        result.append(character);
                    }
                }
            }
        }
        return result.append('"').toString();
    }

    /** Convert the finite IEEE-754 value to ECMAScript's JSON number spelling. */
    private static String number(String source) {
        final double value;
        try {
            value = Double.parseDouble(source);
        } catch (NumberFormatException exception) {
            throw new IllegalArgumentException("invalid JSON number: " + source, exception);
        }
        if (!Double.isFinite(value)) {
            throw new IllegalArgumentException("JCS rejects non-finite number: " + source);
        }
        if (value == 0.0d) {
            return "0";
        }
        if (Math.abs(value) == Double.MIN_VALUE) {
            return value < 0.0d ? "-5e-324" : "5e-324";
        }

        String text = Double.toString(value).toLowerCase(java.util.Locale.ROOT);
        boolean negative = text.charAt(0) == '-';
        if (negative) {
            text = text.substring(1);
        }
        int exponentMarker = text.indexOf('e');
        int exponent = 0;
        String significand = text;
        if (exponentMarker >= 0) {
            significand = text.substring(0, exponentMarker);
            exponent = Integer.parseInt(text.substring(exponentMarker + 1));
        }
        int dot = significand.indexOf('.');
        int decimalPosition = dot < 0 ? significand.length() : dot;
        String digits = dot < 0
            ? significand
            : significand.substring(0, dot) + significand.substring(dot + 1);
        while (digits.length() > 1 && digits.charAt(digits.length() - 1) == '0' && dot >= 0) {
            digits = digits.substring(0, digits.length() - 1);
        }
        decimalPosition += exponent;

        double absolute = Math.abs(value);
        String result;
        if (absolute >= 1e-6d && absolute < 1e21d) {
            if (decimalPosition <= 0) {
                result = "0." + "0".repeat(-decimalPosition) + digits;
            } else if (decimalPosition >= digits.length()) {
                result = digits + "0".repeat(decimalPosition - digits.length());
            } else {
                result = digits.substring(0, decimalPosition) + "." + digits.substring(decimalPosition);
            }
            if (result.indexOf('.') >= 0) {
                result = result.replaceFirst("0+$", "").replaceFirst("\\.$", "");
            }
        } else {
            int scientificExponent = decimalPosition - 1;
            result = digits.charAt(0) == 0 ? digits : String.valueOf(digits.charAt(0));
            if (digits.length() > 1) {
                result += "." + digits.substring(1);
            }
            if (result.indexOf('.') >= 0) {
                result = result.replaceFirst("0+$", "").replaceFirst("\\.$", "");
            }
            result += "e" + (scientificExponent >= 0 ? "+" : "") + scientificExponent;
        }
        return negative ? "-" + result : result;
    }

    private static MessageDigest newDigest() {
        try {
            return MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 is unavailable", exception);
        }
    }

    private static byte[] digest(byte[] value) {
        MessageDigest digest = newDigest();
        digest.update(value);
        return digest.digest();
    }

    private static String hex(byte[] value) {
        StringBuilder result = new StringBuilder(value.length * 2);
        for (byte item : value) {
            result.append(Character.forDigit((item >>> 4) & 0x0f, 16));
            result.append(Character.forDigit(item & 0x0f, 16));
        }
        return result.toString();
    }
}
