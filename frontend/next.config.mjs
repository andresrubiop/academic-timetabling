/** @type {import('next').NextConfig} */
const nextConfig = {
  // `standalone` copies only the traced dependencies into .next/standalone,
  // so the runtime image carries no node_modules. On a filesystem with 9 GiB
  // free that is the difference between a ~200 MB image and a ~1.2 GB one.
  output: "standalone",
  // Do not advertise the framework version to anything scanning this shared
  // host. nginx already sets the rest of the security headers; this one can
  // only be removed here.
  poweredByHeader: false,
  reactStrictMode: true,
};

export default nextConfig;
