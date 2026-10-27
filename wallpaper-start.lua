local mp = require "mp"
local msg = require "mp.msg"

local LONG_VIDEO_SECONDS = 30 * 60
local RANDOM_TIME_LIMIT = 0.90
local IMAGE_EXTENSIONS = {
    jpg = true,
    jpeg = true,
    png = true,
    webp = true,
    bmp = true,
    gif = true,
    avif = true,
}
local VIDEO_EXTENSIONS = {
    mp4 = true,
    mkv = true,
    webm = true,
    mov = true,
    avi = true,
    m4v = true,
    wmv = true,
    flv = true,
    mpg = true,
    mpeg = true,
    ts = true,
    m2ts = true,
}
local seeded = false
local framing_signature = nil
local last_drop_time = nil

local function seed_random_once()
    if seeded then
        return
    end
    seeded = true
    local fractional_clock = math.floor((mp.get_time() % 1) * 1000000)
    math.randomseed(os.time() + fractional_clock)
    -- Discard the first few values from older Lua PRNG implementations.
    math.random()
    math.random()
    math.random()
end

local function choose_long_video_start()
    local loop_file = mp.get_property("loop-file", "no")
    if loop_file == "inf" or loop_file == "yes" then
        return
    end
    local duration = mp.get_property_number("duration", 0)
    if not duration or duration <= LONG_VIDEO_SECONDS then
        return
    end

    seed_random_once()
    local chapter_list = mp.get_property_native("chapter-list", {})
    local chapter_times = {}
    if type(chapter_list) == "table" then
        for _, chapter in ipairs(chapter_list) do
            local chapter_time = tonumber(chapter.time)
            if chapter_time and chapter_time >= 0 and chapter_time < duration then
                table.insert(chapter_times, chapter_time)
            end
        end
    end

    local target
    if #chapter_times > 0 then
        target = chapter_times[math.random(#chapter_times)]
    else
        target = math.random() * duration * RANDOM_TIME_LIMIT
    end
    msg.verbose(string.format("long video %.3fs -> seek %.3fs", duration, target))
    mp.commandv("seek", string.format("%.3f", target), "absolute", "exact")
end

local function is_image_path(path)
    local extension = path and path:match("%.([^./\\]+)$")
    return extension and IMAGE_EXTENSIONS[extension:lower()] == true
end

local function is_video_path(path)
    local extension = path and path:match("%.([^./\\]+)$")
    return extension and VIDEO_EXTENSIONS[extension:lower()] == true
end

local function seek_or_step(direction)
    local path = mp.get_property("path", "")
    if is_image_path(path) then
        mp.commandv(direction > 0 and "playlist-next" or "playlist-prev", "weak")
        return
    end
    mp.commandv("seek", direction * 5, "relative")
end

local function adjust_volume(delta)
    mp.commandv("add", "volume", delta)
end

local function on_dropped_files(_name, dropped)
    if type(dropped) ~= "table" or dropped.time == nil then
        return
    end
    if dropped.time == last_drop_time then
        return
    end
    last_drop_time = dropped.time

    local contains_video = false
    if type(dropped.files) == "table" then
        for _, path in ipairs(dropped.files) do
            if is_video_path(path) then
                contains_video = true
                break
            end
        end
    end
    if not contains_video then
        return
    end

    -- The built-in insert-next handler updates the playlist in the same input
    -- cycle. Advance on the next loop turn so the first dropped video starts
    -- immediately and the original random playlist remains behind it.
    mp.set_property("loop-file", "inf")
    mp.add_timeout(0, function()
        mp.commandv("playlist-next", "weak")
    end)
end

local function apply_media_framing()
    local path = mp.get_property("path", "")
    local params = mp.get_property_native("video-params", {})
    local width = tonumber(params.w)
    local height = tonumber(params.h)
    if not width or not height or width <= 0 or height <= 0 then
        return
    end

    local rotation = tonumber(params.rotate) or 0
    if rotation % 180 ~= 0 then
        width, height = height, width
    end
    local signature = table.concat({path, width, height, rotation}, "|")
    if signature == framing_signature then
        return
    end
    framing_signature = signature

    if is_image_path(path) and height > width then
        local side = math.floor(width)
        mp.set_property("video-crop", string.format("%dx%d", side, side))
        mp.set_property_number("panscan", 0.0)
        msg.verbose(string.format(
            "portrait image %dx%d -> centered %dx%d square",
            width,
            height,
            side,
            side
        ))
    else
        mp.set_property("video-crop", "")
        mp.set_property_number("panscan", 1.0)
    end
end

local function on_file_loaded()
    framing_signature = nil
    -- Restore the normal fill mode before probing the new file. This prevents
    -- a portrait image's crop from leaking into the next video or landscape image.
    mp.set_property("video-crop", "")
    mp.set_property_number("panscan", 1.0)
    choose_long_video_start()
    apply_media_framing()
end

mp.register_event("file-loaded", on_file_loaded)
mp.register_event("video-reconfig", apply_media_framing)
mp.observe_property("dropped-files", "native", on_dropped_files)
mp.add_forced_key_binding("LEFT", "usage-float-back", function()
    seek_or_step(-1)
end)
mp.add_forced_key_binding("RIGHT", "usage-float-forward", function()
    seek_or_step(1)
end)
mp.add_forced_key_binding("WHEEL_UP", "usage-float-volume-up", function()
    adjust_volume(5)
end)
mp.add_forced_key_binding("WHEEL_DOWN", "usage-float-volume-down", function()
    adjust_volume(-5)
end)
