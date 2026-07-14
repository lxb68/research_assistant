/* 动态论文详情路由加载期间显示的骨架提示。 */

export default function Loading() {
  return (
    <main className="paper-viewer-page">
      <section className="paper-viewer-panel">
        <p>正在加载论文内容...</p>
      </section>
    </main>
  );
}
