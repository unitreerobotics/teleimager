/*
 * Teleimager LiDAR Bridge (C++)
 *
 * Subscribes to rt/utlidar/cloud_livox_mid360 via Unitree SDK2 (CycloneDDS)
 * and publishes processed data over ZMQ.
 *
 * Build: see lidar_bridge_build/CMakeLists.txt
 *
 * Run:
 *   LD_LIBRARY_PATH=/home/unitree/unitree_sdk2/thirdparty/lib/aarch64 \
 *     ./lidar_bridge [eth0] [--cloud-port 55560] [--range-port 55561]
 *
 * ZMQ output:
 *   cloud-port : uint32 N + N*12 float32 bytes (XYZ)
 *   range-port : JPEG bytes of 128x1024 spherical range image
 */

#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/idl/ros2/PointCloud2_.hpp>

#include <zmq.h>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

using namespace unitree::robot;
using PointCloud2 = sensor_msgs::msg::dds_::PointCloud2_;

// ---------------------------------------------------------------
// ZMQ sockets (global for callback access)
// ---------------------------------------------------------------
static void* g_zmq_ctx   = nullptr;
static void* g_cloud_pub = nullptr;
static void* g_range_pub = nullptr;

static int g_range_H      = 128;
static int g_range_W      = 1024;
static float g_range_max  = 20.0f;
static float g_phi_min    = -7.0f  * (float)M_PI / 180.0f;
static float g_phi_max    = 52.0f  * (float)M_PI / 180.0f;

// ---------------------------------------------------------------
// Point cloud parsing
// ---------------------------------------------------------------
struct XYZ { float x, y, z; };

static std::vector<XYZ> parse_cloud(const PointCloud2* msg) {
    uint32_t n = msg->width() * msg->height();
    if (n == 0) return {};

    const auto& fields = msg->fields();
    uint32_t off_x = 0, off_y = 4, off_z = 8;
    for (const auto& f : fields) {
        if (f.name() == "x") off_x = f.offset();
        else if (f.name() == "y") off_y = f.offset();
        else if (f.name() == "z") off_z = f.offset();
    }

    const uint8_t* data = msg->data().data();
    uint32_t step = msg->point_step();
    std::vector<XYZ> pts(n);
    for (uint32_t i = 0; i < n; ++i) {
        const uint8_t* p = data + i * step;
        std::memcpy(&pts[i].x, p + off_x, 4);
        std::memcpy(&pts[i].y, p + off_y, 4);
        std::memcpy(&pts[i].z, p + off_z, 4);
    }
    return pts;
}

// ---------------------------------------------------------------
// Encode cloud: uint32 N + N*12 float32 bytes
// ---------------------------------------------------------------
static std::vector<uint8_t> encode_cloud(const std::vector<XYZ>& pts) {
    uint32_t n = (uint32_t)pts.size();
    std::vector<uint8_t> buf(4 + n * 12);
    std::memcpy(buf.data(), &n, 4);
    std::memcpy(buf.data() + 4, pts.data(), n * 12);
    return buf;
}

// ---------------------------------------------------------------
// Range image: simple JPEG via libjpeg-turbo (if available)
// Falls back to raw pgm bytes if not.
// ---------------------------------------------------------------
static std::vector<uint8_t> make_range_image(const std::vector<XYZ>& pts) {
    int H = g_range_H, W = g_range_W;
    std::vector<uint8_t> img(H * W, 0);

    for (const auto& p : pts) {
        float r = std::sqrt(p.x*p.x + p.y*p.y + p.z*p.z);
        if (r < 0.01f) continue;
        float theta = std::atan2(p.y, p.x);
        float phi   = std::atan2(p.z, std::sqrt(p.x*p.x + p.y*p.y));
        int col = (int)(((theta + (float)M_PI) / (2.0f*(float)M_PI)) * W);
        int row = (int)(((g_phi_max - phi) / (g_phi_max - g_phi_min)) * H);
        col = std::max(0, std::min(W-1, col));
        row = std::max(0, std::min(H-1, row));
        uint8_t val = (uint8_t)std::min(255.0f, r / g_range_max * 255.0f);
        img[row * W + col] = val;
    }
    return img;  // raw grayscale — client decodes as H x W uint8
}

// ---------------------------------------------------------------
// LiDAR callback
// ---------------------------------------------------------------
static void on_cloud(const void* raw) {
    const PointCloud2* msg = static_cast<const PointCloud2*>(raw);
    auto pts = parse_cloud(msg);
    if (pts.empty()) return;

    // Publish cloud
    auto cloud_buf = encode_cloud(pts);
    zmq_send(g_cloud_pub, cloud_buf.data(), cloud_buf.size(), ZMQ_DONTWAIT);

    // Publish range image (raw grayscale bytes)
    auto range_buf = make_range_image(pts);
    zmq_send(g_range_pub, range_buf.data(), range_buf.size(), ZMQ_DONTWAIT);
}

// ---------------------------------------------------------------
// Main
// ---------------------------------------------------------------
int main(int argc, char* argv[]) {
    std::string iface     = "eth0";
    int cloud_port        = 55560;
    int range_port        = 55561;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--cloud-port" && i+1 < argc) cloud_port = std::stoi(argv[++i]);
        else if (arg == "--range-port" && i+1 < argc) range_port = std::stoi(argv[++i]);
        else if (arg[0] != '-') iface = arg;
    }

    // ZMQ setup
    g_zmq_ctx   = zmq_ctx_new();
    g_cloud_pub = zmq_socket(g_zmq_ctx, ZMQ_PUB);
    g_range_pub = zmq_socket(g_zmq_ctx, ZMQ_PUB);
    zmq_bind(g_cloud_pub, ("tcp://*:" + std::to_string(cloud_port)).c_str());
    zmq_bind(g_range_pub, ("tcp://*:" + std::to_string(range_port)).c_str());
    std::cout << "[LidarBridge] ZMQ cloud:" << cloud_port
              << "  range:" << range_port << std::endl;

    // DDS setup
    ChannelFactory::Instance()->Init(0, iface.c_str());
    ChannelSubscriberPtr<PointCloud2> sub(
        new ChannelSubscriber<PointCloud2>("rt/utlidar/cloud_livox_mid360"));
    sub->InitChannel(on_cloud, 1);
    std::cout << "[LidarBridge] Subscribed to rt/utlidar/cloud_livox_mid360"
              << " on " << iface << " — waiting for data..." << std::endl;

    while (true) sleep(1);

    zmq_close(g_cloud_pub);
    zmq_close(g_range_pub);
    zmq_ctx_destroy(g_zmq_ctx);
    return 0;
}
