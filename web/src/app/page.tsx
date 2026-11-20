import { Dashboard } from "@/components/dashboard";

export const metadata = {
  title: "Dashboard",
};

export default function DashboardPage() {
  return (
    <main className="space-y-8 px-4 py-8 sm:px-6 lg:px-8">
      <Dashboard />
    </main>
  );
}
