// SPDX-License-Identifier: Apache-2.0
// Portable unit driver only. Not a customer executable or ZIP/path parser.
#include "inflate_fixed.hpp"
#include <fstream>
#include <iostream>
#include <string>
int main(int argc, char** argv) {
    if (argc != 5) return 2;
    try {
        std::ifstream source(argv[1], std::ios::binary);
        if (!source) return 2;
        const auto compressed = std::stoull(argv[2]), expanded = std::stoull(argv[3]);
        const auto cancel_at = std::stoull(argv[4]);
        unsigned long long steps = 0;
        vse::inflate_exact(compressed, expanded,
            [&](unsigned char* data, unsigned count) -> unsigned {
                source.read(reinterpret_cast<char*>(data), count);
                return static_cast<unsigned>(source.gcount());
            },
            [](const unsigned char* data, unsigned count) {
                std::cout.write(reinterpret_cast<const char*>(data), count);
                if (!std::cout) throw std::runtime_error("output error");
            },
            [&] { if (cancel_at && ++steps >= cancel_at) throw std::runtime_error("cancelled"); });
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n'; return 1;
    }
}
