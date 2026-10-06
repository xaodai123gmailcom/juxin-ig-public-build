import { ArrowUpRight, Compass, Globe2, Layers3, ShieldCheck, Users } from "lucide-react";
import "./home-workspace.css";

const guides = [
  { n: "01", title: "连接你的账号", href: "#/accounts", link: "管理账号", icon: Users, color: "violet" },
  { n: "02", title: "安排日常工作", href: "#/follow-monitor", link: "开始检查", icon: Compass, color: "teal" },
  { n: "03", title: "发现与筛选", href: "#/collection", link: "进入采集", icon: ShieldCheck, color: "rose" },
  { n: "04", title: "跟进与复盘", href: "#/reports", link: "查看报表", icon: Layers3, color: "amber" },
];

export function HomeWorkspace() {
  return <div className="juxin-home">
    <section className="home-hero" aria-labelledby="home-title">
      <div className="home-hero-copy">
        <h1 id="home-title">聚鑫国际</h1>
        <div className="home-hero-actions">
          <a className="home-primary" href="#/accounts">进入账号工作区 <ArrowUpRight size={20} /></a>
          <a className="home-secondary" href="#home-guide">快捷入口 <span>↓</span></a>
        </div>
      </div>
      <div className="home-world" aria-hidden="true">
        <div className="home-world-orbit orbit-one" /><div className="home-world-orbit orbit-two" />
        <div className="home-globe"><Globe2 strokeWidth={.45} /></div>
      </div>
    </section>
    <section className="home-guide" id="home-guide" aria-labelledby="home-guide-title">
      <div className="home-guide-heading"><h2 id="home-guide-title">快捷入口</h2></div>
      <div className="home-guide-grid">{guides.map(({ n, title, href, link, icon: Icon, color }) => <article className={`home-guide-card ${color}`} key={n}>
        <div className="home-guide-card-top"><Icon size={23} /><span>{n}</span></div>
        <h3>{title}</h3><a href={href}>{link}<ArrowUpRight size={18} /></a>
      </article>)}</div>
    </section>
  </div>;
}
