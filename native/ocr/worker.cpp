// Native VSE OCR prototype. Frontend derived from PaddlePaddle PaddleX
// (Apache-2.0). This implementation keeps the pinned PP-OCRv6 FP32 models and
// actual pipeline overrides.
#include "clipper.hpp"
#include "json.hpp"
#include "onnxruntime_c_api.h"
#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <numeric>
#include <opencv2/core.hpp>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>
#include <thread>
#ifdef __linux__
#include <signal.h>
#include <sys/prctl.h>
#endif
#ifndef _WIN32
#include <unistd.h>
#else
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <tlhelp32.h>
#include <windows.h>
#endif
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif
using J = nlohmann::json;
namespace fs = std::filesystem;
const OrtApi *api = nullptr;
void check(OrtStatus *s) {
  if (s) {
    std::string m = api->GetErrorMessage(s);
    api->ReleaseStatus(s);
    throw std::runtime_error(m);
  }
}
struct Value {
  OrtValue *p = nullptr;
  ~Value() {
    if (p)
      api->ReleaseValue(p);
  }
};
struct Info {
  OrtTensorTypeAndShapeInfo *p = nullptr;
  ~Info() {
    if (p)
      api->ReleaseTensorTypeAndShapeInfo(p);
  }
};
struct Tensor {
  std::vector<float> data;
  std::vector<int64_t> shape;
};
struct Session {
  OrtSession *s = nullptr;
  std::string input, output;
  Session(OrtEnv *env, const fs::path &path, int threads) {
    OrtSessionOptions *o = nullptr;
    check(api->CreateSessionOptions(&o));
    try {
      check(api->SetIntraOpNumThreads(o, threads));
      check(api->SetInterOpNumThreads(o, 1));
      check(api->SetSessionGraphOptimizationLevel(o, ORT_ENABLE_ALL));
      check(api->CreateSession(env, path.c_str(), o, &s));
      api->ReleaseSessionOptions(o);
    } catch (...) {
      api->ReleaseSessionOptions(o);
      throw;
    }
    OrtAllocator *a = nullptr;
    check(api->GetAllocatorWithDefaultOptions(&a));
    char *n = nullptr;
    check(api->SessionGetInputName(s, 0, a, &n));
    input = n;
    a->Free(a, n);
    check(api->SessionGetOutputName(s, 0, a, &n));
    output = n;
    a->Free(a, n);
  }
  ~Session() {
    if (s)
      api->ReleaseSession(s);
  }
  Tensor run(Tensor &x, int timeout) {
    OrtMemoryInfo *m = nullptr;
    check(api->CreateCpuMemoryInfo(OrtArenaAllocator, OrtMemTypeDefault, &m));
    Value in, out;
    auto status = api->CreateTensorWithDataAsOrtValue(
        m, x.data.data(), x.data.size() * 4, x.shape.data(), x.shape.size(),
        ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &in.p);
    api->ReleaseMemoryInfo(m);
    check(status);
    OrtRunOptions *r = nullptr;
    check(api->CreateRunOptions(&r));
    std::mutex mutex;
    std::condition_variable cv;
    bool done = false;
    std::thread timer([&] {
      std::unique_lock<std::mutex> lock(mutex);
      if (!cv.wait_for(lock, std::chrono::milliseconds(timeout),
                       [&] { return done; })) {
        auto e = api->RunOptionsSetTerminate(r);
        if (e)
          api->ReleaseStatus(e);
      }
    });
    const char *ins[] = {input.c_str()}, *outs[] = {output.c_str()};
    const OrtValue *ivs[] = {in.p};
    status = api->Run(s, r, ins, ivs, 1, outs, 1, &out.p);
    {
      std::lock_guard<std::mutex> lock(mutex);
      done = true;
    }
    cv.notify_all();
    timer.join();
    api->ReleaseRunOptions(r);
    check(status);
    Info info;
    check(api->GetTensorTypeAndShape(out.p, &info.p));
    size_t rank, n;
    check(api->GetDimensionsCount(info.p, &rank));
    Tensor y;
    y.shape.resize(rank);
    check(api->GetDimensions(info.p, y.shape.data(), rank));
    check(api->GetTensorShapeElementCount(info.p, &n));
    if (n > 100000000)
      throw std::runtime_error("oversized output");
    void *p = nullptr;
    check(api->GetTensorMutableData(out.p, &p));
    y.data.assign((float *)p, (float *)p + n);
    return y;
  }
};
using Quad = std::array<cv::Point2f, 4>;
template <class T> std::pair<Quad, float> mini(const std::vector<T> &contour) {
  auto rect = cv::minAreaRect(contour);
  Quad p;
  rect.points(p.data());
  std::stable_sort(p.begin(), p.end(),
                   [](auto &a, auto &b) { return a.x < b.x; });
  int a = 0, d = 1, b = 2, c = 3;
  if (p[1].y <= p[0].y) {
    a = 1;
    d = 0;
  }
  if (p[3].y <= p[2].y) {
    b = 3;
    c = 2;
  }
  return {{p[a], p[b], p[c], p[d]},
          std::min(rect.size.width, rect.size.height)};
}
float score(const cv::Mat &pred, const Quad &q) {
  float xmin = q[0].x, xmax = xmin, ymin = q[0].y, ymax = ymin;
  for (auto p : q) {
    xmin = std::min(xmin, p.x);
    xmax = std::max(xmax, p.x);
    ymin = std::min(ymin, p.y);
    ymax = std::max(ymax, p.y);
  }
  int x0 = std::clamp((int)floor(xmin), 0, pred.cols - 1),
      x1 = std::clamp((int)ceil(xmax), 0, pred.cols - 1),
      y0 = std::clamp((int)floor(ymin), 0, pred.rows - 1),
      y1 = std::clamp((int)ceil(ymax), 0, pred.rows - 1);
  cv::Mat mask = cv::Mat::zeros(y1 - y0 + 1, x1 - x0 + 1, CV_8U);
  std::vector<cv::Point> p;
  for (auto v : q)
    p.emplace_back((int)(v.x - x0), (int)(v.y - y0));
  cv::fillPoly(mask, std::vector<std::vector<cv::Point>>{p}, 1);
  return cv::mean(pred(cv::Rect(x0, y0, x1 - x0 + 1, y1 - y0 + 1)), mask)[0];
}
std::vector<Quad> detect(Tensor &y, int width, int height, int max_candidates,
                         bool sort_output = true, double thresh = .3,
                         double box_thresh = .6, double unclip_ratio = 1.5) {
  if (y.shape.size() != 4 || y.shape[0] != 1 || y.shape[1] != 1)
    throw std::runtime_error("invalid detector shape");
  int h = y.shape[2], w = y.shape[3];
  cv::Mat pred(h, w, CV_32F, y.data.data()), mask;
  cv::compare(pred, thresh, mask, cv::CMP_GT);
  std::vector<std::vector<cv::Point>> contours;
  cv::findContours(mask, contours, cv::RETR_LIST, cv::CHAIN_APPROX_SIMPLE);
  std::vector<Quad> boxes;
  for (size_t i = 0; i < std::min(contours.size(), size_t(max_candidates));
       ++i) {
    auto [q, side] = mini(contours[i]);
    if (side < 3 || score(pred, q) < box_thresh)
      continue;
    std::vector<cv::Point2f> poly(q.begin(), q.end());
    double len = cv::arcLength(poly, true);
    if (len <= 0)
      continue;
    double dist = cv::contourArea(poly) * unclip_ratio / len;
    ClipperLib::Path path;
    for (auto p : q)
      path.emplace_back((ClipperLib::cInt)p.x, (ClipperLib::cInt)p.y);
    ClipperLib::ClipperOffset off;
    off.AddPath(path, ClipperLib::jtRound, ClipperLib::etClosedPolygon);
    ClipperLib::Paths paths;
    off.Execute(paths, dist);
    std::vector<cv::Point> expanded;
    for (auto &ps : paths)
      for (auto p : ps)
        expanded.emplace_back(p.X, p.Y);
    if (expanded.empty())
      continue;
    auto [b, s] = mini(expanded);
    if (s < 5)
      continue;
    for (auto &p : b) {
      p.x = std::clamp(std::nearbyint(double(p.x) * width / w), 0.,
                       double(width));
      p.y = std::clamp(std::nearbyint(double(p.y) * height / h), 0.,
                       double(height));
    }
    boxes.push_back(b);
  }
  if (!sort_output)
    return boxes;
  std::stable_sort(boxes.begin(), boxes.end(), [](auto &a, auto &b) {
    return a[0].y == b[0].y ? a[0].x < b[0].x : a[0].y < b[0].y;
  });
  for (int i = 0; i < (int)boxes.size() - 1; ++i)
    for (int j = i; j >= 0; --j) {
      if (std::abs(boxes[j + 1][0].y - boxes[j][0].y) < 10 &&
          boxes[j + 1][0].x < boxes[j][0].x)
        std::swap(boxes[j], boxes[j + 1]);
      else
        break;
    }
  return boxes;
}
cv::Mat crop(const cv::Mat &im, const Quad &q) {
  std::vector<cv::Point> points;
  for (auto p : q)
    points.emplace_back((int)p.x, (int)p.y);
  auto b = mini(points).first;
  auto length = [](cv::Point2f p) { return std::sqrt(p.x * p.x + p.y * p.y); };
  int w = std::max(length(b[0] - b[1]), length(b[2] - b[3])),
      h = std::max(length(b[0] - b[3]), length(b[1] - b[2]));
  if (w < 1 || h < 1)
    return {};
  Quad dst = {cv::Point2f(0, 0), cv::Point2f(w, 0), cv::Point2f(w, h),
              cv::Point2f(0, h)};
  cv::Mat out;
  cv::warpPerspective(im, out,
                      cv::getPerspectiveTransform(b.data(), dst.data()),
                      cv::Size(w, h), cv::INTER_CUBIC, cv::BORDER_REPLICATE);
  if (h / double(w) >= 1.5)
    cv::rotate(out, out, cv::ROTATE_90_COUNTERCLOCKWISE);
  return out;
}
struct Entry {
  Quad q;
  cv::Mat image;
  std::string text;
  float confidence = 0;
};
J normalize(std::vector<Entry> &entries) {
  struct Row {
    double center, height;
    std::vector<int> indices;
  };
  std::vector<std::array<int, 4>> bounds;
  for (auto &e : entries) {
    int x0 = e.q[0].x, x1 = x0, y0 = e.q[0].y, y1 = y0;
    for (auto p : e.q) {
      x0 = std::min(x0, (int)p.x);
      x1 = std::max(x1, (int)p.x);
      y0 = std::min(y0, (int)p.y);
      y1 = std::max(y1, (int)p.y);
    }
    bounds.push_back({x0, x1, y0, y1});
  }
  std::vector<int> ids(entries.size());
  std::iota(ids.begin(), ids.end(), 0);
  std::stable_sort(ids.begin(), ids.end(), [&](int a, int b) {
    return std::tie(bounds[a][2], bounds[a][0]) <
           std::tie(bounds[b][2], bounds[b][0]);
  });
  std::vector<Row> rows;
  for (int i : ids) {
    auto b = bounds[i];
    if (b[1] <= b[0] || b[3] <= b[2])
      continue;
    double center = (b[2] + b[3]) / 2., height = b[3] - b[2];
    auto it = std::find_if(rows.begin(), rows.end(), [&](auto &r) {
      return std::abs(center - r.center) <=
             std::max(5., std::min(height, r.height) * .5);
    });
    if (it == rows.end())
      rows.push_back({center, height, {i}});
    else
      it->indices.push_back(i);
  }
  std::stable_sort(rows.begin(), rows.end(),
                   [](auto &a, auto &b) { return a.center < b.center; });
  J boxes = J::array(), recognition = J::array();
  for (auto &r : rows) {
    std::stable_sort(r.indices.begin(), r.indices.end(),
                     [&](int a, int b) { return bounds[a][0] < bounds[b][0]; });
    for (int i : r.indices) {
      auto b = bounds[i];
      boxes.push_back({{b[0], b[2]}, {b[1], b[2]}, {b[1], b[3]}, {b[0], b[3]}});
      recognition.push_back({entries[i].text, entries[i].confidence});
    }
  }
  return J::array({boxes, recognition});
}
struct Engine {
  Session det;
  std::unique_ptr<Session> rec;
  OrtEnv *environment;
  fs::path rec_path;
  int threads;
  std::vector<std::string> chars;
  std::array<float, 3> alpha, beta;
  int rec_h, rec_w, max_candidates;
  int det_limit = 64;
  bool det_max = false;
  double det_thresh = .3, det_box_thresh = .6, det_unclip = 1.5;
  Engine(OrtEnv *env, const fs::path &models, const fs::path &configs,
         const std::string &tier, const std::string &rec_name, int thread_count,
         bool detect_only)
      : det(env, models / ("PP-OCRv6_" + tier + "_det_infer.onnx"),
            thread_count),
        environment(env), rec_path(models / (rec_name + "_infer.onnx")),
        threads(thread_count) {
    std::ifstream f(configs / (tier + ".json"));
    J cfg;
    f >> cfg;
    if (detect_only) {
      det_limit = 960;
      det_max = true;
      for (auto &op : cfg["det"]["PreProcess"]["transform_ops"])
        if (op.contains("DetResizeForTest") &&
            op["DetResizeForTest"].is_object()) {
          auto resize = op["DetResizeForTest"];
          det_limit = resize.value("resize_long", 960);
          auto kind = resize.value("limit_type", std::string("max"));
          if (kind != "min" && kind != "max")
            throw std::runtime_error("unsupported standalone resize profile");
          det_max = kind == "max";
        }
      auto post = cfg["det"]["PostProcess"];
      det_thresh = post.value("thresh", .3);
      det_box_thresh = post.value("box_thresh", .6);
      det_unclip = post.value("unclip_ratio", 2.);
    }
    if (!detect_only && rec_name != "PP-OCRv6_" + tier + "_rec") {
      auto rec_config =
          (rec_name == "PP-OCRv6_small_rec" ? configs / "small.json"
           : rec_name == "PP-OCRv6_medium_rec"
               ? configs / "medium.json"
               : configs / (rec_name + ".json"));
      std::ifstream rf(rec_config);
      if (!rf)
        throw std::runtime_error("recognizer config missing: " + rec_name);
      J rc;
      rf >> rc;
      cfg["rec"] = rc.contains("rec") ? rc["rec"] : rc;
    }
    if (!detect_only && !fs::is_regular_file(rec_path))
      throw std::runtime_error("recognizer model missing: " + rec_name);
    chars.push_back("blank");
    for (auto &c : cfg["rec"]["PostProcess"]["character_dict"])
      chars.push_back(c.get<std::string>());
    chars.push_back(" ");
    for (auto &op : cfg["rec"]["PreProcess"]["transform_ops"])
      if (op.contains("RecResizeImg")) {
        rec_h = op["RecResizeImg"]["image_shape"][1];
        rec_w = op["RecResizeImg"]["image_shape"][2];
      }
    for (auto &op : cfg["det"]["PreProcess"]["transform_ops"])
      if (op.contains("NormalizeImage")) {
        auto n = op["NormalizeImage"];
        for (int c = 0; c < 3; ++c) {
          double mean = n["mean"][c], sd = n["std"][c];
          alpha[c] = (1. / 255) / sd;
          beta[c] = -mean / sd;
        }
      }
    max_candidates = cfg["det"]["PostProcess"].value("max_candidates", 1000);
  }
  J predict(const cv::Mat &src, int timeout, bool detect_only, int batch_size) {
    auto deadline =
        std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout);
    auto remaining = [&]() {
      auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                    deadline - std::chrono::steady_clock::now())
                    .count();
      if (ms <= 0)
        throw std::runtime_error("request inference deadline exceeded");
      return int(ms);
    };
    cv::Mat im = src;
    if (im.rows + im.cols < 64) {
      im = cv::Mat::zeros(std::max(32, src.rows), std::max(32, src.cols),
                          src.type());
      src.copyTo(im(cv::Rect(0, 0, src.cols, src.rows)));
    }
    double ratio = 1.;
    if (det_max && std::max(im.rows, im.cols) > det_limit)
      ratio = double(det_limit) / std::max(im.rows, im.cols);
    if (!det_max && std::min(im.rows, im.cols) < det_limit)
      ratio = double(det_limit) / std::min(im.rows, im.cols);
    int h = int(im.rows * ratio), w = int(im.cols * ratio);
    if (std::max(h, w) > 4000) {
      ratio = 4000. / std::max(h, w);
      h = int(h * ratio);
      w = int(w * ratio);
    }
    h = std::max(32, int(std::nearbyint(h / 32.)) * 32);
    w = std::max(32, int(std::nearbyint(w / 32.)) * 32);
    cv::Mat resized;
    cv::resize(im, resized, cv::Size(w, h));
    Tensor x;
    x.shape = {1, 3, h, w};
    x.data.resize(3 * h * w);
    for (int y = 0; y < h; ++y)
      for (int xx = 0; xx < w; ++xx)
        for (int c = 0; c < 3; ++c) {
          float v = resized.at<cv::Vec3b>(y, xx)[c];
          v *= alpha[c];
          v += beta[c];
          x.data[c * h * w + y * w + xx] = v;
        }
    auto pred = det.run(x, remaining());
    auto boxes = detect(pred, src.cols, src.rows, max_candidates, !detect_only,
                        det_thresh, det_box_thresh, det_unclip);
    if (detect_only) {
      J polys = J::array();
      for (auto &q : boxes) {
        J poly = J::array();
        for (auto &p : q)
          poly.push_back({int(p.x), int(p.y)});
        polys.push_back(poly);
      }
      return polys;
    }
    if (!rec)
      rec = std::make_unique<Session>(environment, rec_path, threads);
    std::vector<Entry> entries;
    for (auto &q : boxes) {
      auto image = crop(src, q);
      if (!image.empty())
        entries.push_back({q, image});
    }
    std::vector<int> ids(entries.size());
    std::iota(ids.begin(), ids.end(), 0);
    std::stable_sort(ids.begin(), ids.end(), [&](int a, int b) {
      return entries[a].image.cols / double(entries[a].image.rows) <
             entries[b].image.cols / double(entries[b].image.rows);
    });
    for (size_t start = 0; start < ids.size(); start += batch_size) {
      int n = std::min(size_t(batch_size), ids.size() - start), bw = 0;
      std::vector<int> widths;
      for (int b = 0; b < n; ++b) {
        auto &image = entries[ids[start + b]].image;
        int width = std::min(
            3200, int(rec_h * std::max(rec_w / double(rec_h),
                                       image.cols / double(image.rows))));
        widths.push_back(width);
        bw = std::max(bw, width);
      }
      // Known OCR recognizers have at most ceil(width/4)+8 timesteps.
      // Refuse oversized user batches before ORT allocates its output tensor.
      if (size_t(n) * (size_t(bw) / 4 + 8) * chars.size() > 100000000)
        throw std::runtime_error(
            "recognition batch exceeds output memory limit");
      Tensor batch;
      batch.shape = {n, 3, rec_h, bw};
      if (size_t(n) * 3 * rec_h * bw > 64000000)
        throw std::runtime_error(
            "recognition batch exceeds input memory limit");
      batch.data.resize(size_t(n) * 3 * rec_h * bw, 0);
      for (int b = 0; b < n; ++b) {
        auto &image = entries[ids[start + b]].image;
        double wh = image.cols / double(image.rows);
        int rw = rec_h * std::max(rec_w / double(rec_h), wh) > 3200
                     ? 3200
                     : std::min(widths[b], int(ceil(rec_h * wh)));
        cv::Mat small;
        cv::resize(image, small, cv::Size(rw, rec_h));
        for (int y = 0; y < rec_h; ++y)
          for (int xx = 0; xx < rw; ++xx)
            for (int c = 0; c < 3; ++c) {
              float v = small.at<cv::Vec3b>(y, xx)[c];
              v /= 255.f;
              v -= .5f;
              v /= .5f;
              batch.data[((b * 3 + c) * rec_h + y) * bw + xx] = v;
            }
      }
      auto output = rec->run(batch, remaining());
      if (output.shape.size() != 3 || output.shape[0] != n ||
          output.shape[2] != (int64_t)chars.size())
        throw std::runtime_error("invalid recognizer shape/dictionary");
      int steps = output.shape[1], classes = output.shape[2];
      for (int b = 0; b < n; ++b) {
        auto &e = entries[ids[start + b]];
        int previous = -1, count = 0;
        float sum = 0;
        for (int t = 0; t < steps; ++t) {
          auto begin = output.data.begin() + (b * steps + t) * classes;
          int best = std::max_element(begin, begin + classes) - begin;
          if (best && best != previous) {
            e.text += chars[best];
            sum += begin[best];
            ++count;
          }
          previous = best;
        }
        e.confidence = count ? sum / count : 0;
      }
    }
    return normalize(entries);
  }
};
cv::Mat read_image(const std::string &path) {
  if (path.size() > 8192 || path.find('\0') != std::string::npos)
    throw std::runtime_error("invalid image path");
  fs::path p = fs::u8path(path);
  std::ifstream f(p, std::ios::binary | std::ios::ate);
  if (!f)
    throw std::runtime_error("cannot open image");
  auto size = f.tellg();
  if (size <= 0 || size > 32 * 1024 * 1024)
    throw std::runtime_error("image byte size out of bounds");
  std::vector<unsigned char> data((size_t)size);
  f.seekg(0);
  if (!f.read((char *)data.data(), size))
    throw std::runtime_error("image read failed");
  auto im = cv::imdecode(data, cv::IMREAD_COLOR);
  if (im.empty())
    throw std::runtime_error("invalid image");
  if (im.total() > 16000000)
    throw std::runtime_error("image pixel limit exceeded");
  return im;
}
void env_set(const char *k, const char *v) {
#ifdef _WIN32
  _putenv_s(k, v);
#else
  setenv(k, v, 1);
#endif
}
unsigned long expected_parent_pid() {
  const char *e = std::getenv("VSE_OCR_PARENT_PID");
  if (!e)
    return 0;
  std::string v(e);
  if (v.empty() || v.find_first_not_of("0123456789") != std::string::npos)
    throw std::runtime_error("invalid VSE_OCR_PARENT_PID");
  auto n = std::stoull(v);
  if (n == 0 || n > 0xffffffffULL)
    throw std::runtime_error("VSE_OCR_PARENT_PID out of bounds");
  return (unsigned long)n;
}
// Protect against abrupt multiprocessing-parent termination, including during
// model loading/decode. Windows uses a retained process HANDLE (not a reused
// PID).
struct ParentGuard {
#if defined(_WIN32)
  HANDLE parent = nullptr, stop = nullptr;
  std::thread watcher;
  ParentGuard() {
    DWORD parent_id = 0;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE)
      throw std::runtime_error("cannot inspect parent process");
    PROCESSENTRY32 pe{};
    pe.dwSize = sizeof(pe);
    if (Process32First(snap, &pe))
      do {
        if (pe.th32ProcessID == GetCurrentProcessId()) {
          parent_id = pe.th32ParentProcessID;
          break;
        }
      } while (Process32Next(snap, &pe));
    CloseHandle(snap);
    if (!parent_id)
      throw std::runtime_error("cannot identify parent process");
    auto expected = expected_parent_pid();
    if (expected && expected != parent_id)
      throw std::runtime_error("parent identity changed before worker startup");
    parent = OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE,
                         parent_id);
    if (!parent)
      throw std::runtime_error("cannot monitor parent process");
    FILETIME pc, px, pk, pu, cc, cx, ck, cu;
    if (!GetProcessTimes(parent, &pc, &px, &pk, &pu) ||
        !GetProcessTimes(GetCurrentProcess(), &cc, &cx, &ck, &cu) ||
        CompareFileTime(&pc, &cc) >= 0) {
      CloseHandle(parent);
      throw std::runtime_error("parent process identity cannot be verified");
    }
    stop = CreateEvent(nullptr, TRUE, FALSE, nullptr);
    if (!stop) {
      CloseHandle(parent);
      throw std::runtime_error("cannot create parent monitor event");
    }
    watcher = std::thread([this] {
      HANDLE handles[] = {parent, stop};
      if (WaitForMultipleObjects(2, handles, FALSE, INFINITE) == WAIT_OBJECT_0)
        TerminateProcess(GetCurrentProcess(), 125);
    });
  }
  ~ParentGuard() {
    SetEvent(stop);
    watcher.join();
    CloseHandle(parent);
    CloseHandle(stop);
  }
#elif defined(__linux__)
  ParentGuard() {
    auto parent = getppid();
    auto expected = expected_parent_pid();
    if (expected && expected != (unsigned long)parent)
      throw std::runtime_error("parent identity changed before worker startup");
    if (prctl(PR_SET_PDEATHSIG, SIGKILL) != 0)
      throw std::runtime_error("cannot establish parent-death protection");
    if (getppid() != parent)
      _exit(125);
  }
#else
  std::mutex mutex;
  std::condition_variable condition;
  bool stop = false;
  std::thread watcher;
  ParentGuard() {
    auto parent = getppid();
    auto expected = expected_parent_pid();
    if (expected && expected != (unsigned long)parent)
      throw std::runtime_error("parent identity changed before worker startup");
    watcher = std::thread([this, parent] {
      std::unique_lock<std::mutex> lock(mutex);
      while (!condition.wait_for(lock, std::chrono::milliseconds(100),
                                 [this] { return stop; }))
        if (getppid() != parent)
          _exit(125);
    });
  }
  ~ParentGuard() {
    {
      std::lock_guard<std::mutex> lock(mutex);
      stop = true;
    }
    condition.notify_all();
    watcher.join();
  }
#endif
};
int bounded_int(const J &r, const char *key, int fallback, int low, int high) {
  if (!r.contains(key))
    return fallback;
  if (!r[key].is_number_integer())
    throw std::runtime_error(std::string(key) + " must be integer");
  auto v = r[key].get<int64_t>();
  if (v < low || v > high)
    throw std::runtime_error(std::string(key) + " out of bounds");
  return int(v);
}
int default_threads() {
  const char *value = std::getenv("VSE_OCR_CPU_THREADS");
  if (!value)
    return 4;
  std::string v(value);
  if (v.empty() || v.find_first_not_of("0123456789") != std::string::npos)
    throw std::runtime_error("invalid VSE_OCR_CPU_THREADS");
  auto n = std::stoll(v);
  if (n < 1 || n > 128)
    throw std::runtime_error("VSE_OCR_CPU_THREADS out of bounds");
  return int(n);
}
bool model_name_valid(const std::string &name) {
  return !name.empty() && name.size() < 128 &&
         !(name.size() >= 6 &&
           name.compare(name.size() - 6, 6, "_infer") == 0) &&
         name.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRST"
                                "UVWXYZ0123456789_-") == std::string::npos;
}
#ifdef _WIN32
int wmain(int argc, wchar_t **argv) {
#else
int main(int argc, char **argv) {
#endif
  env_set("ORT_DISABLE_TELEMETRY", "1");
  env_set("OPENCV_IO_MAX_IMAGE_PIXELS", "16000000");
  env_set("OPENCV_IO_MAX_IMAGE_WIDTH", "16384");
  env_set("OPENCV_IO_MAX_IMAGE_HEIGHT", "16384");
#ifdef _WIN32
  _setmode(_fileno(stdin), _O_BINARY);
  _setmode(_fileno(stdout), _O_BINARY);
#endif
  try {
    ParentGuard parent_guard;
    int env_threads = default_threads();
    fs::path models, config;
    for (int i = 1; i < argc; i += 2) {
      if (i + 1 >= argc)
        throw std::runtime_error("missing argument");
#ifdef _WIN32
      if (std::wstring(argv[i]) == L"--models")
        models = fs::path(argv[i + 1]);
      else if (std::wstring(argv[i]) == L"--config")
        config = fs::path(argv[i + 1]);
#else
      if (std::string(argv[i]) == "--models")
        models = fs::u8path(argv[i + 1]);
      else if (std::string(argv[i]) == "--config")
        config = fs::u8path(argv[i + 1]);
#endif
      else
        throw std::runtime_error("unknown argument");
    }
    if (models.empty() || config.empty())
      throw std::runtime_error("usage: native-ocr --models DIR --config DIR");
    api = OrtGetApiBase()->GetApi(ORT_API_VERSION);
    if (!api)
      throw std::runtime_error("ORT API unavailable");
    OrtEnv *env = nullptr;
    check(api->CreateEnv(ORT_LOGGING_LEVEL_WARNING, "vse-native-ocr", &env));
    auto guard = std::unique_ptr<OrtEnv, void (*)(OrtEnv *)>(
        env, [](OrtEnv *p) { api->ReleaseEnv(p); });
    check(api->DisableTelemetryEvents(env));
    std::unique_ptr<Engine> engine;
    std::string engine_key;
    for (;;) {
      unsigned char header[4];
      std::cin.read((char *)header, 4);
      if (std::cin.gcount() == 0 && std::cin.eof())
        return 0;
      if (std::cin.gcount() != 4)
        throw std::runtime_error("truncated frame header");
      uint32_t size = uint32_t(header[0]) | (uint32_t(header[1]) << 8) |
                      (uint32_t(header[2]) << 16) | (uint32_t(header[3]) << 24);
      if (size > 65536)
        throw std::runtime_error("frame exceeds 65536 bytes");
      std::string payload(size, '\0');
      if (!std::cin.read(payload.data(), size))
        throw std::runtime_error("truncated frame body");
      J result = {{"id", nullptr}, {"ok", false}};
      try {
        J request =
            J::parse(payload, [](int depth, J::parse_event_t, const J &) {
              if (depth > 32)
                throw std::runtime_error("JSON nesting limit exceeded");
              return true;
            });
        if (!request.is_object())
          throw std::runtime_error("request must be object");
        if (request.contains("id"))
          result["id"] = request["id"];
        for (auto it = request.begin(); it != request.end(); ++it)
          if (it.key() != "id" && it.key() != "tier" && it.key() != "path" &&
              it.key() != "timeout_ms" && it.key() != "provider" &&
              it.key() != "operation" && it.key() != "batch_size" &&
              it.key() != "cpu_threads" && it.key() != "rec_model")
            throw std::runtime_error("unknown request field: " + it.key());
        if (request.value("provider", std::string("cpu")) != "cpu")
          throw std::runtime_error(
              "only CPU provider is validated; GPU request refused");
        auto tier = request.at("tier").get<std::string>();
        if (tier != "small" && tier != "medium")
          throw std::runtime_error("unsupported tier");
        int timeout = bounded_int(request, "timeout_ms", 30000, 1, 120000);
        int batch_size = bounded_int(request, "batch_size", 6, 1, 100);
        int cpu_threads =
            bounded_int(request, "cpu_threads", env_threads, 1, 128);
        std::string operation = request.value("operation", std::string("ocr"));
        if (operation != "ocr" && operation != "detect")
          throw std::runtime_error("unsupported operation");
        bool detect_only = operation == "detect";
        std::string rec_name =
            request.value("rec_model", "PP-OCRv6_" + tier + "_rec");
        if (!model_name_valid(rec_name))
          throw std::runtime_error("invalid recognizer model name");
        auto image = read_image(request.at("path").get<std::string>());
        auto key = tier + ":" + rec_name + ":" + std::to_string(cpu_threads) +
                   ":" + operation;
        if (!engine || key != engine_key) {
          engine.reset();
          engine = std::make_unique<Engine>(env, models, config, tier, rec_name,
                                            cpu_threads, detect_only);
          engine_key = key;
        }
        auto prediction =
            engine->predict(image, timeout, detect_only, batch_size);
        result[detect_only ? "dt_polys" : "result"] = prediction;
        result["cpu_threads"] = cpu_threads;
        result["batch_size"] = batch_size;
        result["rec_model"] = rec_name;
        result["text_order"] =
            (rec_name.rfind("arabic_", 0) == 0 ? "logical" : "display");
        result["ok"] = true;
      } catch (const std::exception &e) {
        result["error"] = e.what();
      }
      std::string out =
          result.dump(-1, ' ', false, J::error_handler_t::replace);
      uint32_t n = out.size();
      unsigned char h[4] = {(unsigned char)n, (unsigned char)(n >> 8),
                            (unsigned char)(n >> 16), (unsigned char)(n >> 24)};
      std::cout.write((char *)h, 4);
      std::cout.write(out.data(), out.size());
      std::cout.flush();
      if (!std::cout)
        return 0;
    }
  } catch (const std::exception &e) {
    std::cerr << "native-ocr: " << e.what() << '\n';
    return 2;
  }
}
