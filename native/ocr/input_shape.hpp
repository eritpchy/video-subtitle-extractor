// Read only ONNX input metadata. No model rewriting, protobuf/runtime dependency,
// graph evaluation, or initializer allocation. All wire ranges are file-bounded.
#pragma once
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace native_shape {
struct Dimension { int64_t value = 0; std::string symbol; };
struct Input { std::string name; std::vector<Dimension> dimensions; };
struct Range { uint64_t begin, end; };
struct Field { unsigned number, wire; uint64_t integer = 0; Range bytes{}; };
class Reader {
  std::ifstream file_;
  uint64_t size_ = 0;
  size_t field_count_ = 0;
  uint64_t varint(uint64_t &position, uint64_t end) {
    uint64_t value = 0;
    for (unsigned shift = 0; shift < 70; shift += 7) {
      if (position >= end) throw std::runtime_error("truncated ONNX varint");
      const int byte = file_.get();
      if (byte < 0 || (shift == 63 && (byte & 0xfe)))
        throw std::runtime_error("invalid ONNX varint");
      ++position;
      value |= uint64_t(byte & 0x7f) << shift;
      if (!(byte & 0x80)) return value;
    }
    throw std::runtime_error("oversized ONNX varint");
  }
public:
  explicit Reader(const std::filesystem::path &path)
      : file_(path, std::ios::binary | std::ios::ate) {
    if (!file_) throw std::runtime_error("cannot read ONNX input metadata");
    const auto length = file_.tellg();
    if (length <= 0 || length > std::streamoff(1024ULL * 1024 * 1024))
      throw std::runtime_error("ONNX metadata file size out of bounds");
    size_ = static_cast<uint64_t>(length);
  }
  Range whole() const { return {0, size_}; }
  std::vector<Field> fields(Range range) {
    if (range.begin > range.end || range.end > size_)
      throw std::runtime_error("invalid ONNX field range");
    std::vector<Field> result;
    uint64_t position = range.begin;
    while (position < range.end) {
      if (++field_count_ > 1000000)
        throw std::runtime_error("ONNX metadata field count exceeded");
      file_.seekg(position);
      if (!file_) throw std::runtime_error("cannot seek ONNX metadata");
      const auto key = varint(position, range.end);
      if (!(key >> 3) || (key >> 3) > 0x1fffffff)
        throw std::runtime_error("invalid ONNX field number");
      Field field{static_cast<unsigned>(key >> 3), static_cast<unsigned>(key & 7)};
      uint64_t length = 0;
      if (field.wire == 0) field.integer = varint(position, range.end);
      else if (field.wire == 2) length = varint(position, range.end);
      else if (field.wire == 1) length = 8;
      else if (field.wire == 5) length = 4;
      else throw std::runtime_error("unsupported ONNX metadata wire type");
      if (length > range.end - position)
        throw std::runtime_error("truncated ONNX metadata field");
      field.bytes = {position, position + length};
      position += length;
      result.push_back(field);
    }
    return result;
  }
  Field required(Range range, unsigned number, unsigned wire) {
    Field result{};
    bool found = false;
    for (auto field : fields(range)) if (field.number == number) {
      if (found || field.wire != wire)
        throw std::runtime_error("duplicated or mistyped ONNX metadata field");
      found = true; result = field;
    }
    if (!found) throw std::runtime_error("missing ONNX input metadata field");
    return result;
  }
  std::string string(Range range) {
    const auto length = range.end - range.begin;
    if (!length || length > 256)
      throw std::runtime_error("invalid ONNX input metadata string length");
    std::string value(length, '\0');
    file_.seekg(range.begin);
    if (!file_.read(value.data(), length) || value.find('\0') != std::string::npos)
      throw std::runtime_error("invalid ONNX input metadata string");
    return value;
  }
};
inline Input read_input(const std::filesystem::path &path) {
  Reader reader(path);
  const auto graph = reader.required(reader.whole(), 7, 2).bytes;
  const auto input = reader.required(graph, 11, 2).bytes;
  Input result;
  result.name = reader.string(reader.required(input, 1, 2).bytes);
  const auto type = reader.required(input, 2, 2).bytes;
  const auto tensor = reader.required(type, 1, 2).bytes;
  if (reader.required(tensor, 1, 0).integer != 1)
    throw std::runtime_error("native OCR requires a FLOAT32 ONNX input");
  const auto shape = reader.required(tensor, 2, 2).bytes;
  for (auto field : reader.fields(shape)) {
    if (field.number != 1) continue;
    if (field.wire != 2) throw std::runtime_error("invalid ONNX dimension type");
    Dimension dimension;
    bool found = false;
    for (auto item : reader.fields(field.bytes)) {
      if (item.number != 1 && item.number != 2) continue;
      if (found) throw std::runtime_error("ambiguous ONNX dimension");
      found = true;
      if (item.number == 1 && item.wire == 0 && item.integer > 0 &&
          item.integer <= INT64_MAX) dimension.value = static_cast<int64_t>(item.integer);
      else if (item.number == 2 && item.wire == 2)
        dimension.symbol = reader.string(item.bytes);
      else throw std::runtime_error("invalid ONNX dimension");
    }
    if (!found) throw std::runtime_error("anonymous ONNX input dimension cannot be bound");
    result.dimensions.push_back(dimension);
  }
  if (result.dimensions.size() != 4)
    throw std::runtime_error("native OCR requires a rank-four ONNX input");
  return result;
}
inline std::map<std::string, int64_t> overrides(
    const Input &input, const std::vector<int64_t> &shape) {
  if (shape.size() != 4 || input.dimensions.size() != shape.size())
    throw std::runtime_error("native OCR input shape rank mismatch");
  std::map<std::string, int64_t> result;
  uint64_t count = 1;
  for (size_t i = 0; i < shape.size(); ++i) {
    if (shape[i] < 1 || uint64_t(shape[i]) > 64000000 / count)
      throw std::runtime_error("native OCR bound input shape exceeds memory limit");
    count *= shape[i];
    const auto &dimension = input.dimensions[i];
    if (dimension.symbol.empty()) {
      if (dimension.value != shape[i])
        throw std::runtime_error("native OCR input disagrees with fixed ONNX dimension");
    } else {
      const auto inserted = result.emplace(dimension.symbol, shape[i]);
      if (!inserted.second && inserted.first->second != shape[i])
        throw std::runtime_error("conflicting ONNX symbolic dimension values");
    }
  }
  return result;
}
} // namespace native_shape
