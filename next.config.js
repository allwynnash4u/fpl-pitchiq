/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [
      { source: "/", destination: "/index.html" },
      { source: "/api/:path*", destination: "https://pitchiq-python-production.up.railway.app/api/:path*" }
    ];
  }
};

module.exports = nextConfig;
