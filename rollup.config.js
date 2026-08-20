import deckyPlugin from "@decky/rollup";

const config = deckyPlugin({});
config.output.sourcemap = false;
delete config.output.sourcemapPathTransform;

export default config;
