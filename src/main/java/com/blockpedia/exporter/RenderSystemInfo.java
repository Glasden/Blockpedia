package com.blockpedia.exporter;

import com.mojang.blaze3d.systems.DeviceInfo;
import com.mojang.blaze3d.systems.RenderSystem;
import net.minecraft.client.Minecraft;

final class RenderSystemInfo {
    private RenderSystemInfo() {
    }

    static Device device(Minecraft minecraft) {
        return new Device(
            RenderSystem.getDevice().getDeviceInfo().vendorName(),
            RenderSystem.getDevice().getDeviceInfo().name(),
            RenderSystem.getDevice().getDeviceInfo().driverInfo(),
            RenderSystem.getDevice().getDeviceInfo().backendName()
        );
    }

    static String describe(Minecraft minecraft) {
        Device device = device(minecraft);
        return device.vendor() + "|" + device.name() + "|" + device.driver() + "|" + device.backend();
    }

    record Device(String vendor, String name, String driver, String backend) {
    }
}
