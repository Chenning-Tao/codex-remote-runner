import { readFile } from "node:fs/promises";

const packageJson = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url), "utf8"),
);
const reactVersion = packageJson.dependencies?.react;
const reactDomVersion = packageJson.dependencies?.["react-dom"];

if (!reactVersion || reactVersion !== reactDomVersion) {
  throw new Error(
    `react and react-dom must use the same exact version; found ${reactVersion ?? "missing"} and ${reactDomVersion ?? "missing"}`,
  );
}
