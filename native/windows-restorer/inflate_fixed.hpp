// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <limits>
#include <stdexcept>
#include <vector>
#include "third_party/zlib-1.3.2/zlib.h"

namespace vse {
// Raw DEFLATE only; no archive/file-name/gzip parser. Both sizes are immutable
// build metadata bound to the SHA256 of the complete accepted original ZIP.
class InflateState {
    struct alignas(std::max_align_t) Allocation { std::size_t bytes; };
    static constexpr std::size_t allocation_limit = 1024 * 1024;
    std::size_t allocated_ = 0;
    bool ready_ = false;
    static voidpf allocate(voidpf opaque, uInt items, uInt size) noexcept {
        auto self = static_cast<InflateState*>(opaque);
        if (size && items > (std::numeric_limits<std::size_t>::max)() / size) return nullptr;
        const auto bytes = static_cast<std::size_t>(items) * size;
        if (bytes > allocation_limit - self->allocated_) return nullptr;
        auto result = static_cast<Allocation*>(std::calloc(1, sizeof(Allocation) + bytes));
        if (!result) return nullptr;
        result->bytes = bytes; self->allocated_ += bytes;
        return result + 1;
    }
    static void release(voidpf opaque, voidpf value) noexcept {
        if (!value) return;
        auto self = static_cast<InflateState*>(opaque);
        auto allocation = static_cast<Allocation*>(value) - 1;
        self->allocated_ -= allocation->bytes;
        std::free(allocation);
    }
public:
    z_stream stream{};
    InflateState() {
        stream.zalloc = allocate; stream.zfree = release; stream.opaque = this;
        if (inflateInit2(&stream, -15) != Z_OK) throw std::runtime_error("Cannot initialize bounded raw inflate");
        ready_ = true;
    }
    ~InflateState() { if (ready_) inflateEnd(&stream); }
    InflateState(const InflateState&) = delete;
    InflateState& operator=(const InflateState&) = delete;
};

template<class Read, class Write, class Cancel>
void inflate_exact(std::uint64_t compressed, std::uint64_t expanded, Read read, Write write, Cancel cancel) {
    constexpr uInt chunk_bytes = 1024 * 1024;
    std::vector<unsigned char> input(chunk_bytes), output(chunk_bytes);
    InflateState state;
    auto& stream = state.stream;
    std::uint64_t remaining = compressed, produced_total = 0;
    for (;;) {
        cancel();
        if (!stream.avail_in && remaining) {
            const auto take = static_cast<uInt>((std::min)(remaining, static_cast<std::uint64_t>(chunk_bytes)));
            if (read(input.data(), take) != take) throw std::runtime_error("Truncated fixed compressed range");
            remaining -= take; stream.next_in = input.data(); stream.avail_in = take;
        }
        const auto input_before = stream.avail_in;
        // At most one byte beyond the fixed output size is decoded to detect an
        // oversized stream. It is rejected before WriteFile/hash receives it.
        const auto left = expanded - produced_total;
        const auto capacity = left < chunk_bytes ? static_cast<uInt>(left) + 1 : chunk_bytes;
        stream.next_out = output.data(); stream.avail_out = capacity;
        const int status = inflate(&stream, Z_NO_FLUSH);
        const auto produced = capacity - stream.avail_out;
        if (produced > left) throw std::runtime_error("DEFLATE exceeds fixed output size");
        if (produced) { write(output.data(), produced); produced_total += produced; }
        if (status == Z_STREAM_END) {
            if (remaining || stream.avail_in || produced_total != expanded) throw std::runtime_error("DEFLATE range has trailing input or wrong output size");
            return;
        }
        if (status != Z_OK && status != Z_BUF_ERROR) throw std::runtime_error("Invalid raw DEFLATE stream");
        if (input_before == stream.avail_in && !produced) throw std::runtime_error("Raw DEFLATE made no progress");
    }
}
} // namespace vse
