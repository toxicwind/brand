#pragma once

#include <filesystem>
#include <string>
#include <string_view>

#include <hyprland/src/desktop/view/Window.hpp>

namespace brand {

struct ScreenshotResult {
    bool        success = false;
    std::string error;
};

ScreenshotResult captureScreenshotSession(const std::filesystem::path& outputJsonPath, const PHLWINDOW& targetWindow = {});

} // namespace brand
