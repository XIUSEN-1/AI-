import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, setToken } from "@/lib/api";

interface AuthResp {
  token: string;
  user: { id: number; name: string; role: string };
}

export default function LoginPage() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [studentNo, setStudentNo] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const resp = await api<AuthResp>("/api/auth/student", {
        method: "POST",
        body: JSON.stringify({ name, student_no: studentNo, invite_code: inviteCode || null }),
      });
      setToken(resp.token);
      navigate("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败，请重试");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-xl">AI 能力罗盘</CardTitle>
          <CardDescription>输入姓名与学号即可开始测评（邀请码选填）</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={handleSubmit}>
            <div className="space-y-2">
              <Label htmlFor="name">姓名</Label>
              <Input id="name" value={name} onChange={(e) => setName(e.target.value)} required maxLength={32} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="studentNo">学号</Label>
              <Input id="studentNo" value={studentNo} onChange={(e) => setStudentNo(e.target.value)} required maxLength={32} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="inviteCode">班级邀请码（选填）</Label>
              <Input id="inviteCode" value={inviteCode} onChange={(e) => setInviteCode(e.target.value)} maxLength={16} />
            </div>
            {error && <p className="text-sm text-red-600">{error}</p>}
            <Button className="w-full" type="submit" disabled={loading}>
              {loading ? "进入中…" : "开始"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
