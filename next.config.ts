import type { NextConfig } from 'next';

const isGcpCloudRunBuild = process.env.GCP_CLOUD_RUN_BUILD === '1';

const nextConfig: NextConfig = {
  distDir: isGcpCloudRunBuild ? '.next-gcp' : '.next',
  output: isGcpCloudRunBuild ? 'standalone' : undefined,
};

export default nextConfig;
