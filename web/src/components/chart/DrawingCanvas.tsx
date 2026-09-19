import React, { useEffect, useRef, useState } from "react";

export type DrawingTool = "cursor" | "trendline" | "ray" | "hline" | "fib" | "rect";

export interface Point {
  x: number;
  y: number;
}

export interface DrawingItem {
  id: string;
  type: DrawingTool;
  points: Point[];
  color?: string;
}

interface DrawingCanvasProps {
  tool: DrawingTool;
  onToolSelect: (tool: DrawingTool) => void;
  width: number;
  height: number;
}

export const DrawingCanvas: React.FC<DrawingCanvasProps> = ({
  tool,
  onToolSelect,
  width,
  height,
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [drawings, setDrawings] = useState<DrawingItem[]>(() => {
    try {
      const saved = localStorage.getItem("atr.chart.drawings");
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });
  const [currentPoints, setCurrentPoints] = useState<Point[]>([]);
  const [isDrawing, setIsDrawing] = useState(false);

  useEffect(() => {
    localStorage.setItem("atr.chart.drawings", JSON.stringify(drawings));
  }, [drawings]);

  // Redraw all items on canvas
  const redraw = (activePreviewPoint?: Point) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    ctx.clearRect(0, 0, width, height);

    const allItems: DrawingItem[] = [...drawings];
    if (currentPoints.length > 0 && activePreviewPoint) {
      allItems.push({
        id: "preview",
        type: tool,
        points: [...currentPoints, activePreviewPoint],
        color: "#2962ff",
      });
    }

    for (const item of allItems) {
      ctx.strokeStyle = item.color || "#2962ff";
      ctx.lineWidth = 2;
      ctx.fillStyle = "rgba(41, 98, 255, 0.15)";

      if (item.type === "hline" && item.points[0]) {
        ctx.beginPath();
        ctx.setLineDash([4, 4]);
        ctx.moveTo(0, item.points[0].y);
        ctx.lineTo(width, item.points[0].y);
        ctx.stroke();
        ctx.setLineDash([]);
      } else if ((item.type === "trendline" || item.type === "ray") && item.points.length >= 2) {
        const [p1, p2] = item.points;
        ctx.beginPath();
        ctx.moveTo(p1.x, p1.y);
        if (item.type === "ray") {
          const dx = p2.x - p1.x;
          const dy = p2.y - p1.y;
          ctx.lineTo(p1.x + dx * 20, p1.y + dy * 20);
        } else {
          ctx.lineTo(p2.x, p2.y);
        }
        ctx.stroke();
      } else if (item.type === "rect" && item.points.length >= 2) {
        const [p1, p2] = item.points;
        const rx = Math.min(p1.x, p2.x);
        const ry = Math.min(p1.y, p2.y);
        const rw = Math.abs(p2.x - p1.x);
        const rh = Math.abs(p2.y - p1.y);
        ctx.fillRect(rx, ry, rw, rh);
        ctx.strokeRect(rx, ry, rw, rh);
      } else if (item.type === "fib" && item.points.length >= 2) {
        const [p1, p2] = item.points;
        const levels = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
        const dy = p2.y - p1.y;
        ctx.lineWidth = 1;

        levels.forEach((lvl) => {
          const y = p1.y + dy * lvl;
          ctx.beginPath();
          ctx.moveTo(Math.min(p1.x, p2.x) - 50, y);
          ctx.lineTo(Math.max(p1.x, p2.x) + 50, y);
          ctx.stroke();
          ctx.fillStyle = "#787b86";
          ctx.font = "10px sans-serif";
          ctx.fillText(`Fib ${lvl} (${(100 * lvl).toFixed(1)}%)`, Math.min(p1.x, p2.x) - 45, y - 3);
        });
      }
    }
  };

  useEffect(() => {
    redraw();
  }, [drawings, width, height]);

  const handleMouseDown = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (tool === "cursor") return;
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;
    const p: Point = { x: e.clientX - rect.left, y: e.clientY - rect.top };

    if (tool === "hline") {
      setDrawings((prev) => [
        ...prev,
        { id: String(Date.now()), type: "hline", points: [p], color: "#ff9800" },
      ]);
      onToolSelect("cursor");
      return;
    }

    if (!isDrawing) {
      setIsDrawing(true);
      setCurrentPoints([p]);
    } else {
      // Finish line / shape
      setDrawings((prev) => [
        ...prev,
        { id: String(Date.now()), type: tool, points: [...currentPoints, p], color: "#2962ff" },
      ]);
      setCurrentPoints([]);
      setIsDrawing(false);
      onToolSelect("cursor");
    }
  };

  const handleMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (!isDrawing || currentPoints.length === 0) return;
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;
    const p: Point = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    redraw(p);
  };

  return (
    <canvas
      ref={canvasRef}
      width={width}
      height={height}
      onMouseDown={handleMouseDown}
      onMouseMove={handleMouseMove}
      className={`absolute inset-0 z-20 ${
        tool !== "cursor" ? "cursor-crosshair pointer-events-auto" : "pointer-events-none"
      }`}
    />
  );
};
