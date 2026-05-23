#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <vector>

#include <xrt/xrt_bo.h>
#include <xrt/xrt_device.h>
#include <xrt/xrt_kernel.h>

int main(int argc, char** argv) {
  if (argc < 2) {
    std::cerr << "usage: host <xclbin>\n";
    return 2;
  }
  const std::string xclbin_path = argv[1];
  const int length = 16;
  std::vector<std::uint32_t> input(length);
  std::vector<std::uint32_t> output(length, 0);
  std::vector<std::uint32_t> expected(length, 0);
  for (int i = 0; i < length; ++i) {
    input[i] = static_cast<std::uint32_t>(i + 1);
    expected[i] = input[i] + 1U;
  }

  auto device = xrt::device(0);
  auto uuid = device.load_xclbin(xclbin_path);
  auto kernel = xrt::kernel(device, uuid, "{{TOP_FUNCTION}}");
  auto in_bo = xrt::bo(device, sizeof(std::uint32_t) * input.size(), kernel.group_id(0));
  auto out_bo = xrt::bo(device, sizeof(std::uint32_t) * output.size(), kernel.group_id(1));
  auto in_map = in_bo.map<std::uint32_t*>();
  auto out_map = out_bo.map<std::uint32_t*>();
  std::copy(input.begin(), input.end(), in_map);
  std::fill(out_map, out_map + output.size(), 0U);
  in_bo.sync(XCL_BO_SYNC_BO_TO_DEVICE);
  out_bo.sync(XCL_BO_SYNC_BO_TO_DEVICE);

  auto run = kernel(in_bo, out_bo, length);
  run.wait();
  out_bo.sync(XCL_BO_SYNC_BO_FROM_DEVICE);
  std::copy(out_map, out_map + output.size(), output.begin());

  bool pass = true;
  for (int i = 0; i < length; ++i) {
    if (output[i] != expected[i]) {
      pass = false;
      break;
    }
  }
  std::cout << "HLS_BOARD_STATUS " << (pass ? "passed" : "failed") << "\n";
  return pass ? 0 : 1;
}
