import { Activity, FlaskConical } from "lucide-react";

type ProductSection = "runs" | "experiments";

export function ProductNav({ active }: { active: ProductSection }) {
  return (
    <nav className="rr-product-nav" aria-label="Remote Runner 分区">
      <a
        href="/"
        className={`rr-product-nav-item ${active === "runs" ? "rr-product-nav-active" : ""}`}
        aria-current={active === "runs" ? "page" : undefined}
      >
        <Activity aria-hidden="true" />运行
      </a>
      <a
        href="/?view=experiments"
        className={`rr-product-nav-item ${active === "experiments" ? "rr-product-nav-active" : ""}`}
        aria-current={active === "experiments" ? "page" : undefined}
      >
        <FlaskConical aria-hidden="true" />实验
      </a>
    </nav>
  );
}
