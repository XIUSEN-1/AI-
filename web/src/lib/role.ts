/** 角色工具：登录用户角色类型与中文标签（权限路由守卫见 App.tsx 的 RequireRole）。 */
export type Role = "student" | "teacher" | "admin";

export const ROLES: Role[] = ["student", "teacher", "admin"];

export function isRole(value: string): value is Role {
  return (ROLES as string[]).includes(value);
}

export const ROLE_LABELS: Record<Role, string> = {
  student: "学员",
  teacher: "教师",
  admin: "管理员",
};
