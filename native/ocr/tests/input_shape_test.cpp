#include "../input_shape.hpp"
#include "json.hpp"
#include <iostream>
int main(int argc, char **argv) {
  try {
    if (argc != 6) throw std::runtime_error("expected model path and four dimensions");
    auto input = native_shape::read_input(argv[1]);
    std::vector<int64_t> shape;
    for (int i = 2; i < argc; ++i) shape.push_back(std::stoll(argv[i]));
    auto overrides = native_shape::overrides(input, shape);
    std::cout << nlohmann::json({{"input", input.name}, {"overrides", overrides},
                               {"shape", shape}}).dump() << '\n';
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 2;
  }
}
